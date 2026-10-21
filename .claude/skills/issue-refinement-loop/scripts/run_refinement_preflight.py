#!/usr/bin/env python3
"""
run_refinement_preflight.py

Deterministic entrypoint that assembles planner input from GitHub API data,
validates anchor comments structurally, invokes plan_refinement_loop.py with
correctly-formed stdin JSON, and writes a compact result artifact.

Usage:
    uv run python3 run_refinement_preflight.py \\
        --issue-number <N> \\
        --repo <owner/name> \\
        [--anchor-comment-url <URL> ...] \\
        [--fixture <path>]

Output (stdout): compact projection of refinement_preflight_result/v1 artifact.

Canonical stdout fields:
    STATUS       - pass | warn | needs_fix | blocked | environment_failure (always present)
    NEXT_ACTION  - routing instruction (always present)
    MUST_READ    - files/paths to read before proceeding (omitted if empty)
    COMMANDS     - argv-only command templates (omitted if empty)
    BLOCKERS     - blocker reason codes (omitted if empty)
    ARTIFACT     - artifact key: absolute_path pairs (omitted if empty)
    REQUIRED_SECTIONS - required sections from rewrite constraints (planner-derived)
    REQUIRED_CONTRACT_KEYS - required contract keys from rewrite constraints (planner-derived)
    REWRITE_CONSTRAINTS - planner rewrite constraints payload when fail_closed=true
    REPAIR_ACTION - versioned repair_action disposition (Issue #2016; omitted
                    unless STATUS: needs_fix, i.e. disposition: auto_apply_safe)

Non-canonical / suppressed fields:
    SUMMARY      - human-only prose, not consumed by orchestrators
    DO_NOT_READ  - reserved, currently empty; consumers MUST NOT rely on absence
    EVIDENCE     - raw issue body / comments; NEVER emitted to stdout

Artifact (file):  .claude/artifacts/issue-refinement-loop/<issue_number>/
                  refinement_preflight_result_v1.json  (canonical result)
                  raw_issue_snapshot.json              (raw issue + comments)
                  planner_input.json                   (planner stdin, byte-stable)

Exit codes:
    0 - pass (planner succeeded, fail_closed.required == false, no unknown confidence)
    1 - warn (planner exit 0, fail_closed.required == false, >=1 decision with
              confidence: unknown — human note needed but not blocking)
    2 - blocked (anchor mismatch, planner exit 2, or planner fail_closed.required == true)
    3 - environment_failure (gh not found / auth / API / timeout / non-JSON)
    4 - needs_fix (repair_issue_contract classified >=1 known-safe deterministic
                    repair as auto_apply_safe; Issue #2016)

Planner ↔ Wrapper Exit Code Mapping:
    anchor comment not in issue                    → blocked  / 2
    gh not found / auth / API fail / timeout / JSON → environment_failure / 3
    planner exit 2 (invalid input)                  → blocked  / 2
    planner exit 3 (internal error)                 → environment_failure / 3
    planner exit 0 + fail_closed.required == true   → blocked  / 2
    planner exit 0, fail_closed=false, no unknown   → pass     / 0
    planner exit 0, fail_closed=false, >=1 unknown  → warn     / 1

warn (exit 1) definition:
    planner exit 0 AND fail_closed.required == false
    AND decisions.*.confidence contains at least one "unknown"
    → status: warn / exit 1 (human note needed, but not fully blocking)

needs_fix (exit 4) definition (Issue #2016):
    repair_issue_contract.py's repair_action.disposition == "auto_apply_safe"
    (>=1 known-safe deterministic repair, no unsafe/unknown/mixed/overlapping
    repair present) AND no other blocker (anchor/env/planner) is present
    → status: needs_fix / exit 4, next_action: apply_deterministic_repair.
    This route is orthogonal to the pre-existing pass/warn/blocked/
    environment_failure mapping above: it only overrides an otherwise
    pass/warn outcome, and never overrides blocked. Other repair_action
    dispositions (human_review_required / informational / invalid_payload)
    do NOT change the mapping below: human_review_required routes through
    the pre-existing generic repair_diagnostics blocker (→ blocked), and
    invalid_payload / subprocess-level repair failures route through
    BLOCKER_REPAIR_ENVIRONMENT_FAILURE (→ environment_failure).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import importlib.metadata
import io
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Runtime schema validation (jsonschema >= 4.0, already in pyproject.toml)
# ---------------------------------------------------------------------------

try:
    import jsonschema as _jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False


# ---------------------------------------------------------------------------
# #1677 AC4/AC12: reuse plan_refinement_loop.py's normative semantic
# validator instead of re-implementing ISSUE_EXECUTION_DECISION_V1
# invariants here. Import is best-effort (subprocess/CLI callers of this
# module do not require it; only _join_scope_rollup_into_planner_input's
# self-check below uses it).
# ---------------------------------------------------------------------------

import sys as _sys_for_import

_sys_for_import.path.insert(0, str(Path(__file__).resolve().parent))
try:
    # PR #1767 owner review (P0-4/AC12 Scope Delta): import the standalone
    # canonical module directly rather than re-exporting through
    # plan_refinement_loop.py, so every consumer shares one authority.
    from validate_issue_execution_decision import validate_issue_execution_decision
except ImportError:  # pragma: no cover - defensive fallback
    validate_issue_execution_decision = None

try:
    # #1891: pure analyzer for anchor comment multi-turn segmentation and
    # candidate extraction. anchor_context.py has no GitHub API client of its
    # own; it only consumes the already-fetched anchor_comment.snapshot body
    # that this module builds below (AC8).
    import anchor_context
except ImportError:  # pragma: no cover - defensive fallback
    anchor_context = None

try:
    # Issue #2016: producer-side repair_action disposition classifier.
    # Imported directly (not re-implemented here) so the wrapper and the
    # standalone CLI share a single source of truth for the closed-enum
    # disposition rule.
    from repair_issue_contract import classify_repair_action
except ImportError:  # pragma: no cover - defensive fallback
    classify_repair_action = None

try:
    # Issue #995 fix_delta (P0-1, OWNER adversarial review): connects the
    # template-derived structural-repair producer to this production
    # preflight execution path. Imported directly (a pure function, no
    # subprocess isolation needed, unlike the repair_action.apply mutation
    # lane) so run_preflight() and the standalone unit tests share the SAME
    # closed-enum bundle/routing functions.
    from repair_issue_contract import (
        build_structural_repair_bundle,
        route_structural_repair_disposition,
        STRUCTURAL_REPAIR_ROUTE_STATUS_PASS,
        STRUCTURAL_REPAIR_ROUTE_STATUS_NEEDS_FIX,
        # Issue #2396: the structural_repair_action.apply consumer and the
        # severity-arbitration override both need the SAME closed-enum
        # disposition literal the producer itself uses (never an
        # independently-declared string that could silently diverge).
        STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
        detect_missing_template_sections,
        # Issue #2431: the known_scalars/source_spans assembler below reuses
        # these SAME primitives (never a re-declared regex/parser) so the
        # producer and the assembler always agree on what counts as a valid
        # `parent_issue` scalar / a parsed `## Heading` section.
        _mrc_parse,
        MRC_PARSE_STATUS_OK,
        _parse_h2_sections,
        # PR #2469 fix_delta iteration 3 (P1): the SAME line-bound helper
        # `_apply_insertion_decision()` uses (repair_issue_contract.py) to
        # compute a section's raw (start_line, end_line) span -- never a
        # re-derived offset arithmetic -- so the assembler's `line_start`/
        # `line_end` always point at the section's ACTUAL raw line range
        # instead of assuming content starts on the line immediately after
        # the heading (false for the common heading -> blank line -> bullets
        # shape).
        _section_line_bounds,
        _DERIVED_SCALAR_FIELD_VALIDATORS,
    )
except ImportError:  # pragma: no cover - defensive fallback
    build_structural_repair_bundle = None
    route_structural_repair_disposition = None
    STRUCTURAL_REPAIR_ROUTE_STATUS_PASS = "pass"
    STRUCTURAL_REPAIR_ROUTE_STATUS_NEEDS_FIX = "needs_fix"
    STRUCT_DISPOSITION_AUTO_APPLY_SAFE = "auto_apply_safe"
    detect_missing_template_sections = None
    _mrc_parse = None
    MRC_PARSE_STATUS_OK = "ok"
    _parse_h2_sections = None
    _section_line_bounds = None
    _DERIVED_SCALAR_FIELD_VALIDATORS = {}



_IMPL_MAIN_DRIFT_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts" / "agent-ops"
if str(_IMPL_MAIN_DRIFT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_MAIN_DRIFT_SCRIPTS_DIR))

# Issue #2241 AC8 / PR #2247 review P1-1 introduced
# `github_credentialless_read.py` (scripts/agent-guards/), an unauthenticated
# GET-only REST transport, for a Claude-GPT launcher that did not forward any
# GitHub auth. Issue #2299 (PR #2303) changed that launcher contract: the
# isolated child now shares GitHub auth (an ambient `GH_CONFIG_DIR` pinned
# before isolation plus any `GH_TOKEN`/`GITHUB_TOKEN`/`GH_HOST`/`GH_REPO`)
# natively, so an isolated `HOME` is NOT evidence that GitHub auth is absent.
# Issue #2872: the production read path therefore always uses native `gh`
# (see `_select_read_transport()`); this module is still imported best-effort
# because the credentialless transport classes/exception taxonomy remain the
# GET-only contract surface (and are exercised directly by its own tests),
# but `_is_isolated_claude_gpt_profile()` no longer selects it.
_AGENT_GUARDS_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_SCRIPTS_DIR))
try:
    import github_credentialless_read as _credentialless_read
except ImportError:  # pragma: no cover - defensive fallback
    _credentialless_read = None
try:
    import repo_identity as _repo_identity
except ImportError:  # fail closed at the binding check, never skipped
    _repo_identity = None
try:
    # Issue #2102 fix_delta (iteration 5, Blocker A): mirrors
    # plan_refinement_loop.py's own best-effort import of the same symbol.
    # This wrapper must not populate known_context["main_drift"] (a bounded
    # live git fetch/diff/merge-tree probe) when the planner it feeds cannot
    # even import the classifier that consumes that key -- a harness that
    # only provisions this skill's own scripts/ directory (not its
    # impl-review-loop sibling) would otherwise pay the live-readback cost
    # only to have plan_refinement_loop.py hard_stop with
    # main_drift_policy_import_failed regardless of the readback's content.
    from main_drift_policy import classify_main_drift as _main_drift_classifier_probe
except ImportError:  # pragma: no cover - defensive fallback
    _main_drift_classifier_probe = None

# Issue #2052: opt-in, phase-scoped SHA-bound evidence reuse for this file's
# own Issue/comment fetch path (`_fetch_issue`/`_fetch_issue_comments`/
# `_fetch_single_comment`) plus the observed-only per-phase metrics report
# that measures it. Both live in `scripts/agent-ops/` (shared, not skill-
# private) -- imported best-effort so a harness that only provisions this
# skill's own `scripts/` directory keeps its pre-existing behavior
# (`evidence_cache_enabled=True` degrades to a no-op fresh-read passthrough
# when this import fails, never to a hard failure).
_AGENT_OPS_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts" / "agent-ops"
if str(_AGENT_OPS_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_SCRIPTS_DIR))
try:
    from evidence_index import EvidenceIndex as _EvidenceIndex
    from evidence_index import RESOURCE_KIND_COMMENT as _EVIDENCE_RESOURCE_KIND_COMMENT
    from evidence_index import RESOURCE_KIND_ISSUE_BODY as _EVIDENCE_RESOURCE_KIND_ISSUE_BODY
    from context_budget_report import ContextBudgetReport as _ContextBudgetReport
except ImportError:  # pragma: no cover - defensive fallback
    _EvidenceIndex = None
    _EVIDENCE_RESOURCE_KIND_COMMENT = "comment"
    _EVIDENCE_RESOURCE_KIND_ISSUE_BODY = "issue_body"
    _ContextBudgetReport = None

# The single phase name used for the live-mode initial Issue/comment fetch
# section of `run_preflight()` below (Issue #2052 AC1/AC2: reuse is bounded
# to this one declared phase of a single process invocation; any other
# phase -- e.g. a post-mutation precondition/readback fetch inside
# `run_repair_action_apply`/`run_structural_repair_action_apply` -- never
# participates in this cache and always reads fresh).
EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH = "preflight_fetch"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SCHEMAS_DIR = _SCRIPTS_DIR.parent / "schemas"
PLANNER_SCRIPT = _SCRIPTS_DIR / "plan_refinement_loop.py"
REPAIR_SCRIPT = _SCRIPTS_DIR / "repair_issue_contract.py"
# Issue #2689: sibling `owner_reaction_decision.py` (Issue #1975), invoked as
# a fresh subprocess immediately before the heavy mutation gate evaluates a
# `not_planned` mutation_category -- see
# `_run_owner_reaction_decision_fresh()` / `_classify_heavy_mutation_gate_with_fresh_owner_reaction()`
# below. Never imported as a Python module (this module never re-implements
# or re-exports owner_reaction_decision.py's selection semantics -- Out of
# Scope for #2689).
OWNER_REACTION_DECISION_SCRIPT = _SCRIPTS_DIR / "owner_reaction_decision.py"

SCHEMA_VERSION_RESULT = "refinement_preflight_result/v1"
SCHEMA_VERSION_PLANNER_INPUT = "refinement_loop_planner_input/v1"
SCHEMA_VERSION_INPUT_FIXTURE = "refinement_preflight_input/v1"

# Timeout constants (seconds)
GH_API_TIMEOUT = 30
PLANNER_TIMEOUT = 60

# PR #2202 review fix-delta (P0-6): `repair_action.apply`'s worst-case inner
# critical path runs the readiness-check subprocess and the
# edit_issue_txn.py subprocess SEQUENTIALLY inside a single
# `run_refinement_preflight.py --apply-repair-action` process invocation.
# The registry-level outer supervisor timeout for the `repair_action.apply`
# command_id (command_registry.py REGISTRY["repair_action.apply"]
# ["timeout_seconds"]) MUST stay strictly greater than
# REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS +
# REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS +
# REPAIR_APPLY_READBACK_RESERVE_SECONDS + margin -- otherwise the outer
# supervisor can kill this process mid-dispatch (after a PATCH may already
# have been sent to GitHub) before the AC5 authoritative-readback path ever
# runs, silently converting a genuinely-mutated Issue into a reported
# failure/crash instead of a resolvable `unknown` outcome.
REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS = 30
REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS = 60
# One bounded `_fetch_issue()` GitHub read (GH_API_TIMEOUT-bounded) is the
# most this function ever spends resolving the AC5 authoritative readback
# after a TimeoutExpired/OSError/unparseable-stdout `unknown` outcome.
REPAIR_APPLY_READBACK_RESERVE_SECONDS = GH_API_TIMEOUT
# inner_total(90) + readback_reserve(30) = 120; command_registry.py's
# `repair_action.apply["timeout_seconds"]` is set to 150 (30s margin above
# that combined worst case) -- see the comment on that registry entry.
REPAIR_APPLY_OUTER_TIMEOUT_MARGIN_SECONDS = 30

# Exit codes
EXIT_PASS = 0
EXIT_WARN = 1
EXIT_BLOCKED = 2
EXIT_ENVIRONMENT_FAILURE = 3
EXIT_NEEDS_FIX = 4

# Blocker reason codes
BLOCKER_ANCHOR_NOT_IN_ISSUE = "ANCHOR_NOT_IN_ISSUE"
BLOCKER_ANCHOR_IS_PR_REVIEW = "ANCHOR_IS_PR_REVIEW_COMMENT"
BLOCKER_GH_FAILURE = "GH_API_FAILURE"
BLOCKER_PLANNER_INVALID_INPUT = "PLANNER_INVALID_INPUT"
BLOCKER_PLANNER_INTERNAL_ERROR = "PLANNER_INTERNAL_ERROR"
BLOCKER_FAIL_CLOSED = "PLANNER_FAIL_CLOSED"
BLOCKER_CONTRACT_UPDATE_FAILED = "CONTRACT_UPDATE_FAILED"
# The transaction layer may return an exception string in `failure` from a
# callback. Never forward it into stdout/artifacts; only these fixed tokens
# are diagnostic authority for the bounded handoff.
CONTRACT_UPDATE_FAILED_REASONS = frozenset({
    "unsafe_unstructured_patch_operation",
    "contract_patch_plan_missing",
    "contract_patch_plan_operations_not_list",
    "invalid_scope_delta_decision_binding",
    "invalid_scope_delta_decision",
    "invalid_patch_plan_producer_unavailable",
    "invalid_operations_key_missing",
    "invalid_operations_not_list",
    "invalid_allowed_path_deltas_not_list",
    "invalid_blank_delta_entry",
    "invalid_anchor_binding_mismatch",
    "invalid_source_evidence_binding_mismatch",
    "invalid_allowed_paths_parser_unavailable",
    "human_review_directive_route_import_failed",
    "human_review_directive_route_classifier_error",
    "human_review_directive_route_fresh_readback_failed",
    "scope_reframe_router_import_failed",
    "scope_delta_status_required_when_allowed_path_deltas_present",
    "candidate_readiness_not_go",
    "anchor_identity_or_trust_changed",
    "body_drift_retry_exhausted",
    "final_readback_postcondition_failed",
    "section_postcondition_failed",
    "post_update_gate_failed",
    "contract_update_failed",
})
BLOCKER_ANCHOR_REPO_MISMATCH = "ANCHOR_REPO_MISMATCH"
BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH = "ANCHOR_ISSUE_NUMBER_MISMATCH"
BLOCKER_ANCHOR_COMMENT_NOT_FOUND = "ANCHOR_COMMENT_NOT_FOUND"
# Issue #2257 AC3: a transport failure (auth dependency, rate limit,
# 5xx, DNS/connection, timeout, malformed JSON, incomplete pagination,
# read authority divergence) resolving the anchor comment must never be
# reported as "comment not found" -- it is a distinct, environment_failure
# blocker (Issue #2197 incident: anchor comment 5315264311 was
# misclassified as missing because of exactly this conflation).
BLOCKER_ANCHOR_FETCH_TRANSPORT_FAILURE = "ANCHOR_FETCH_TRANSPORT_FAILURE"
BLOCKER_ANCHOR_ISSUE_URL_MISMATCH = "ANCHOR_ISSUE_URL_MISMATCH"
BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID = "ANCHOR_COMMENT_SCHEMA_INVALID"
BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED = "ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED"
BLOCKER_INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
BLOCKER_RESULT_SCHEMA_INVALID = "RESULT_SCHEMA_INVALID"
BLOCKER_INVALID_ARGS = "INVALID_ARGS"
BLOCKER_REPOSITORY_IDENTITY_MISMATCH = "REPOSITORY_IDENTITY_MISMATCH"
BLOCKER_REPOSITORY_IDENTITY_UNAVAILABLE = "REPOSITORY_IDENTITY_UNAVAILABLE"
BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD = "REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD"
BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE = "REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE"
BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION = "REWRITE_CONSTRAINTS_INVARIANT_VIOLATION"
BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID = "planner_fail_closed_payload_invalid"
BLOCKER_ARTIFACT_PROJECTION_MISMATCH = "ARTIFACT_PROJECTION_MISMATCH"
BLOCKER_ISSUE_EXECUTION_DECISION_INVALID = "ISSUE_EXECUTION_DECISION_INVALID"
BLOCKER_ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE = "ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE"
# #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the multi-turn-candidate
# route and the heavy-mutation gate must actually reach the `blockers` list
# consumed by `_apply_exit_code_mapping()`, not just live inside
# `known_context` where the planner never inspects them.
BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED = "ANCHOR_MULTI_TURN_FAIL_CLOSED"

# PR #2171 fix_delta (P0-1, OWNER adversarial review): the two distinct
# fail_closed reasons `_apply_multi_turn_candidate_route()` can emit for a
# multi-turn anchor -- the pre-existing hard block
# ("multi_turn_anchor_context_requires_human_judgment") and the
# integrity-unconfirmed forced-blocking route
# ("multi_turn_anchor_context_retrieval_integrity_unconfirmed") -- both
# must surface as BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED downstream.
ANCHOR_MULTI_TURN_FAIL_CLOSED_REASONS = frozenset(
    {
        "multi_turn_anchor_context_requires_human_judgment",
        "multi_turn_anchor_context_retrieval_integrity_unconfirmed",
    }
)
BLOCKER_HEAVY_MUTATION_FAIL_CLOSED = "HEAVY_MUTATION_FAIL_CLOSED"
BLOCKER_REPAIR_ENVIRONMENT_FAILURE = "REPAIR_ENVIRONMENT_FAILURE"


def _render_artifact_projection_lines(artifacts: dict[str, str]) -> list[str]:
    lines: list[str] = ["ARTIFACT:"]
    for key, value in sorted(artifacts.items()):
        lines.append(f"  {key}: {value}")
    return lines


def _validate_artifact_projection(*, repo_root: Path, issue_number: int, artifacts: dict[str, str]) -> list[str]:
    if not artifacts:
        return []

    expected_root = (repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)).resolve()
    failures: list[str] = []

    for key, raw_path in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path:
            failures.append(f"artifact_path_invalid: {key!r}:{raw_path!r}")
            continue

        try:
            resolved = Path(raw_path).resolve()
        except Exception:
            failures.append(f"artifact_path_unresolvable: {key} -> {raw_path!r}")
            continue

        if resolved != expected_root and not resolved.is_relative_to(expected_root):
            normalized = (
                resolved.relative_to(repo_root).as_posix() if resolved.is_relative_to(repo_root) else str(resolved)
            )
            failures.append(f"artifact_path_outside_issue_root: {key} -> {normalized}")
            continue

        if not resolved.exists():
            normalized = (
                resolved.relative_to(repo_root).as_posix() if resolved.is_relative_to(repo_root) else str(resolved)
            )
            failures.append(f"artifact_path_missing: {key} -> {normalized}")

    return failures


# Trusted author associations for ANCHOR_SCOPE_REFRAME_V1
TRUSTED_ANCHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Trusted lanes for source attribution.
_HUMAN_CONTEXT_COMMENT_URLS_FIELD = "human_context_comment_urls"
_AGENT_REPORT_COMMENT_URLS_FIELD = "agent_report_comment_urls"


def _normalize_comment_url_set(value: Any) -> set[str] | None:
    """Return a normalized set of URLs or `None` when malformed.

    Malformed explicit-lane fields are treated as untrusted control-plane
    input (fail-closed) so they cannot be used to infer a trusted origin.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if not isinstance(value, (list, tuple, set)):
        return None
    urls: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        urls.add(item)
    return urls


def _resolve_scope_delta_source_kind(
    anchor_url: str,
    *,
    human_context_comment_urls: Any,
    agent_report_comment_urls: Any,
) -> str:
    """Resolve source kind from control-plane explicit lanes only."""
    human_urls = _normalize_comment_url_set(human_context_comment_urls)
    agent_urls = _normalize_comment_url_set(agent_report_comment_urls)

    # Any unrecognized or unlabeled lane payload is fail-closed.  An anchor
    # URL alone is not an origin lane: callers must state whether it is human
    # context or an agent report.
    if human_urls is None or agent_urls is None:
        return "generated_by_agent"

    in_human = anchor_url in human_urls
    in_agent = anchor_url in agent_urls

    # Fail-closed on duplicate lane identity.
    if in_human and in_agent:
        return "generated_by_agent"

    if in_agent:
        return "generated_by_agent"
    if in_human:
        return "issue_comment"
    return "generated_by_agent"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Compute SHA256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# PR #2202 review fix (P0-2): producer-side provenance-lane determination
# for repair_action.apply. Reuses the SAME control-plane-explicit-lane
# classifier already used for scope-delta authority evidence
# (_resolve_scope_delta_source_kind) rather than inventing a second
# classification path, translated into the repair_action.apply vocabulary
# (human_context/anchor/unanchored, see repair_apply_result_v1.schema.json)
# established by the AC9 fresh-validation lane-preservation check.
def _determine_repair_source_lane(anchor_url: "str | None", known_context: "dict | None") -> str:
    """Resolve the repair-lane provenance the current preflight run executed
    under: 'unanchored' (no anchor comment at all), 'human_context' (an
    operator-labeled human-context anchor), or 'anchor' (an agent-authored /
    unlabeled anchor)."""
    if not anchor_url:
        return "unanchored"
    source_kind = _resolve_scope_delta_source_kind(
        anchor_url,
        human_context_comment_urls=(
            known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD) if isinstance(known_context, dict) else None
        ),
        agent_report_comment_urls=(
            known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD) if isinstance(known_context, dict) else None
        ),
    )
    return "human_context" if source_kind == "issue_comment" else "anchor"


def _repair_source_refs_digest(source_lane: str, anchor_url: "str | None") -> "str | None":
    """Digest binding the source refs (anchor comment URL/id) a repair_action
    producer run relied on. None for the unanchored lane (Issue #2039 AC3)."""
    if source_lane == "unanchored" or not anchor_url:
        return None
    parsed_anchor = _parse_anchor_comment_url(anchor_url)
    return "sha256:" + _sha256(
        _canonical_json({"anchor_url": anchor_url, "comment_id": parsed_anchor.get("comment_id")})
    )


def _repair_preflight_run_identity(
    *, issue_number: int, repo: str, original_body_sha256: str, captured_at: str, source_lane: str
) -> str:
    """A per-run identity for the producer run that generated a
    repair_action candidate (Issue #2039 AC3). Deliberately independent of
    `hashes.result_core_sha256` (which is itself computed from a
    `result_core_for_hash` dict that MAY embed `repair_action_core` -- using
    it here would be self-referential)."""
    return "sha256:" + _sha256(
        _canonical_json(
            {
                "issue_number": issue_number,
                "repo": repo,
                "original_body_sha256": original_body_sha256,
                "captured_at": captured_at,
                "source_lane": source_lane,
            }
        )
    )


# ---------------------------------------------------------------------------
# Issue #2039: repair_action.apply controlled consumer -- foundational
# building blocks. (REPAIR_APPLY_BLOCK_MARKER_ISSUE_2039)
#
# NOTE: the FD-based secure artifact reader (AC7), the exactly-one
# mutation-intent arbiter core (AC1), and the schema migration (AC2,
# repair_apply_result_v1.schema.json / refinement_preflight_result_v1.schema
# .json) are implemented. AC8/AC11 are wired: run_repair_action_apply()
# below is registered as the `repair_action.apply` command_id in
# command_registry.py / skill_runtime_command_policy.py, dispatched by
# skill_runtime_exec.py, and constrained to stdout_contract
# `repair_apply_result/v1`. AC3 (provenance binding), AC4 (pre-dispatch
# rebase-on-drift), AC5 (post-dispatch retry-budget separation +
# authoritative readback), AC6 (lossless receipt projection, phase/
# failure_code separation), AC9 (lane-preserving fresh validation), and AC10
# (historical-artifact immutability + replay-resolves-to-no_change) are also
# implemented -- see run_repair_action_apply()'s own docstring.
# ---------------------------------------------------------------------------


class RepairApplySecureOpenError(RuntimeError):
    """Raised when a repair_apply artifact fails FD-based secure-open
    validation (Issue #2039 AC7)."""


REPAIR_APPLY_MAX_ARTIFACT_BYTES = 1_048_576  # 1 MiB read-size ceiling (AC7)


def _repair_apply_scratch_create(dir_fd: int, name: str, content: bytes) -> None:
    """Create `name` inside the directory referenced by `dir_fd`, ONLY if it
    does not already exist (PR #2202 review P1-1: fixed-name scratch-file
    race/symlink-clobber hardening).

    Uses `O_CREAT | O_EXCL | O_NOFOLLOW` (dir-FD-relative, never a bare
    pathname): `O_EXCL` makes creation fail rather than silently overwrite
    or follow anything already at that name -- including a symlink
    pre-placed by another process with the same permissions, which would
    otherwise redirect this write to clobber an arbitrary writable file
    elsewhere, and would do so BEFORE `edit_issue_txn.py`'s own downstream
    symlink checks ever run. After creation, `fstat()`s the resulting FD
    and verifies it is a genuinely new regular file (`st_nlink == 1`)
    before trusting it, then writes `content` and closes.

    Raises `RepairApplySecureOpenError` (fail-closed) if the name already
    exists, is not a plain regular file post-creation, or has more than
    one hard link.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except FileExistsError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_scratch_already_exists:{name}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RepairApplySecureOpenError(f"repair_apply_scratch_is_symlink:{name}") from exc
        raise RepairApplySecureOpenError(f"repair_apply_scratch_create_error:{name}:{exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise RepairApplySecureOpenError(f"repair_apply_scratch_unsafe_fd:{name}")
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1  # fdopen now owns the fd; do not close twice below
            handle.write(content)
    except RepairApplySecureOpenError:
        raise
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_scratch_write_error:{name}:{exc}") from exc
    finally:
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)


def _repair_apply_scratch_unlink(dir_fd: int, name: str) -> None:
    """Unlink `name` inside the directory referenced by `dir_fd` (PR #2202
    review P1-1). Directory-FD-relative, never a reconstructed pathname
    string, so cleanup cannot be redirected by a symlink swapped into
    place after creation but before cleanup. Best-effort: swallows
    `OSError` (e.g. already removed / never created)."""
    with contextlib.suppress(OSError):
        os.unlink(name, dir_fd=dir_fd)


def _repair_apply_dirfd_open_leaf(path: Path, *, root: Path) -> int:
    """Open `path` for reading using ONLY directory-FD-relative traversal
    from `root` (PR #2202 review P1-2: dir-FD-relative TOCTOU hardening).

    Unlike a pathname-based `lstat()`-then-`open()` pair (which leaves a
    window in which an ancestor directory can be swapped to a symlink
    between the check and the use, redirecting the open outside `root`
    even though the ancestor check "passed" moments earlier), this walks
    every ancestor component with `os.open(..., dir_fd=<already-open
    parent fd>)`. Each step operates on an FD that is already open (not a
    freshly re-resolved pathname), so no ancestor can be substituted
    mid-traversal -- the kernel resolves each component exactly once,
    relative to a directory descriptor whose identity is already fixed.

    `openat2(RESOLVE_BENEATH)` (Linux 5.6+) would be a stronger/simpler
    primitive for this, but CPython's stdlib `os` module does not expose
    it (no `os.openat2()`); adding it would require reaching for a raw
    `ctypes`/syscall shim, which was judged riskier to introduce here than
    this component-wise dir-FD walk. This walk closes the specific
    ancestor-swap TOCTOU window described in review P1-2 without any new
    dependency; it does not implement `RESOLVE_BENEATH`'s additional
    guarantees (e.g. rejecting `..`-escapes mid-resolution at the kernel
    level), which is instead enforced above by explicitly rejecting `..`
    /`.`/empty path components before any FD is opened.

    Every ancestor directory is opened with `O_NOFOLLOW | O_DIRECTORY`
    (rejects a symlink or non-directory masquerading as an ancestor). The
    leaf is opened with `O_NOFOLLOW | O_NONBLOCK` (rejects a leaf symlink
    atomically at open() time via the kernel, and `O_NONBLOCK` prevents an
    indefinite block if the leaf turns out to be a FIFO with no writer --
    such leaves are still rejected as non-regular via the caller's
    post-open `fstat()` check, never trusted).

    Returns an open file descriptor to the leaf; caller owns and must
    close it. Raises `RepairApplySecureOpenError` (fail-closed) for any
    ancestor/leaf that is missing, a symlink, outside `root`, or otherwise
    cannot be opened this way.
    """
    resolved_root = root.resolve(strict=False)
    try:
        rel_parts = path.relative_to(resolved_root).parts
    except ValueError as exc:
        raise RepairApplySecureOpenError(
            f"repair_apply_parent_outside_root:{path}:not_under:{resolved_root}"
        ) from exc
    if not rel_parts:
        raise RepairApplySecureOpenError(f"repair_apply_path_is_root:{path}")
    for part in rel_parts:
        if part in ("", ".", ".."):
            raise RepairApplySecureOpenError(f"repair_apply_path_component_invalid:{path}:{part}")

    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    leaf_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        dir_flags |= os.O_CLOEXEC
        leaf_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        leaf_flags |= os.O_NONBLOCK

    try:
        current_fd = os.open(str(resolved_root), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_root_open_error:{resolved_root}:{exc}") from exc

    opened_fds = [current_fd]
    try:
        for part in rel_parts[:-1]:
            try:
                next_fd = os.open(part, dir_flags, dir_fd=current_fd)
            except FileNotFoundError as exc:
                raise RepairApplySecureOpenError(f"repair_apply_ancestor_missing:{path}:{part}") from exc
            except OSError as exc:
                # ELOOP: kernel refused to resolve a symlink component
                # (O_NOFOLLOW). ENOTDIR: the O_DIRECTORY|O_NOFOLLOW pair
                # requires the final resolved component to already be a
                # directory without following a symlink to get there -- a
                # symlinked ancestor (even one that points at a real
                # directory) fails this exact way on Linux, which is why
                # both errno values are treated as "ancestor is a symlink"
                # here (the two are not distinguished further since either
                # way the ancestor is rejected as unsafe).
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RepairApplySecureOpenError(
                        f"repair_apply_ancestor_is_symlink:{path}:{part}"
                    ) from exc
                raise RepairApplySecureOpenError(
                    f"repair_apply_ancestor_dirfd_open_error:{path}:{part}:{exc}"
                ) from exc
            opened_fds.append(next_fd)
            current_fd = next_fd

        leaf_name = rel_parts[-1]
        try:
            leaf_fd = os.open(leaf_name, leaf_flags, dir_fd=current_fd)
        except FileNotFoundError as exc:
            raise RepairApplySecureOpenError(f"repair_apply_leaf_not_found:{path}") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RepairApplySecureOpenError(f"repair_apply_leaf_is_symlink:{path}") from exc
            if exc.errno == errno.ENXIO:
                raise RepairApplySecureOpenError(
                    f"repair_apply_leaf_not_regular_file:{path}:special_file"
                ) from exc
            raise RepairApplySecureOpenError(f"repair_apply_leaf_open_error:{path}:{exc}") from exc
        return leaf_fd
    finally:
        for fd in opened_fds:
            with contextlib.suppress(OSError):
                os.close(fd)


def secure_read_repair_apply_artifact(
    path: Path,
    *,
    root: Path,
    max_bytes: int = REPAIR_APPLY_MAX_ARTIFACT_BYTES,
    expected_sha256: Optional[str] = None,
) -> "tuple[str, str]":
    """FD-based secure read of a repair_apply artifact (Issue #2039 AC7;
    PR #2202 review P1-2 dir-FD-relative traversal hardening).

    Rejects (fail-closed, before any content bytes are trusted):
      - leaf symlink, FIFO, socket, device, directory (regular files only)
      - parent-directory symlink anywhere between the leaf and `root`
      - parent substitution (resolved parent outside `root`)
      - oversize content (> max_bytes)
      - non-UTF-8 content (strict decode)
      - digest mismatch against `expected_sha256`, when given
      - leaf replaced with a symlink between the read and the post-read
        identity re-check

    The leaf and every ancestor directory between it and `root` are opened
    via `_repair_apply_dirfd_open_leaf()`, i.e. purely through
    directory-FD-relative `open(..., dir_fd=...)` calls chained from an
    already-open `root` FD -- never through a freshly re-resolved
    pathname. This closes the TOCTOU window that a separate
    `lstat()`-then-`open()` pair leaves open (an ancestor directory being
    swapped to a symlink between the ancestor check and the open() call):
    each traversal step operates on an FD whose identity was already
    fixed by the previous step, so there is no window in which a
    concurrent rename/symlink-swap of an ancestor can redirect the open.

    Returns (text, sha256_hex_digest_of_raw_bytes).
    """
    resolved_root = root.resolve(strict=False)
    fd = _repair_apply_dirfd_open_leaf(path, root=resolved_root)

    raw = b""
    try:
        post_st = os.fstat(fd)
        if not stat.S_ISREG(post_st.st_mode):
            raise RepairApplySecureOpenError(f"repair_apply_leaf_not_regular_file_post_open:{path}")
        pre_st = post_st
        if post_st.st_size > max_bytes:
            raise RepairApplySecureOpenError(
                f"repair_apply_leaf_oversize:{path}:{post_st.st_size}:>:{max_bytes}"
            )
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1  # fdopen now owns the fd; do not close twice below
            raw = handle.read(max_bytes + 1)
    except RepairApplySecureOpenError:
        raise
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_read_error:{path}:{exc}") from exc
    finally:
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)

    if len(raw) > max_bytes:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_oversize_on_read:{path}:>:{max_bytes}")

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_not_utf8:{path}:{exc}") from exc

    digest = hashlib.sha256(raw).hexdigest()
    # PR #2202 review fix (P0-2 collateral bug, found by the required
    # canonical-producer E2E test): repair_action.*_body_sha256 fields are
    # always "sha256:<hex>"-prefixed (see repair_issue_contract.py /
    # classify_repair_action()), but every caller here always passed a
    # prefixed expected_sha256 while this compared it against a bare hex
    # digest -- so a REAL producer artifact ALWAYS failed this check
    # (silently masked before now because every hand-built test fixture
    # happened to pass a bare, un-prefixed expected_sha256 instead of a
    # real repair_action's own value).
    if expected_sha256 is not None and digest != _repair_apply_strip_sha_prefix(expected_sha256):
        raise RepairApplySecureOpenError(
            f"repair_apply_digest_mismatch:{path}:expected={expected_sha256}:actual={digest}"
        )

    try:
        after_st = os.lstat(path)
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_post_read_lstat_error:{path}:{exc}") from exc
    if stat.S_ISLNK(after_st.st_mode):
        raise RepairApplySecureOpenError(f"repair_apply_leaf_replaced_with_symlink:{path}")
    if (after_st.st_dev, after_st.st_ino) != (pre_st.st_dev, pre_st.st_ino):
        raise RepairApplySecureOpenError(f"repair_apply_leaf_replaced_post_read:{path}")

    return text, digest


REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS = "multiple_mutation_intents"
REPAIR_APPLY_FAILURE_NO_MUTATION_INTENT = "no_mutation_intent"


def resolve_repair_apply_mutation_intent(
    *,
    contract_update: "dict | None",
    repair_action: "dict | None",
) -> dict:
    """Issue #2039 AC1: exactly-one mutation-intent arbiter for the
    repair_action.apply command lane.

    PR #2202 review fix (P0-1): this arbiter reads the CANONICAL
    `refinement_preflight_result_v1` top-level field `contract_update`
    (emitted by `contract_update.run.with_anchor`) -- not the unrelated,
    schema-invalid `contract_patch_plan` key that a canonical result can
    never carry (`additionalProperties: false`). Reading the wrong field
    made the earlier version of this arbiter always see
    `has_contract_update=False` for real production artifacts, so it could
    never detect the real hazard this AC exists to prevent: a canonical
    result that already carries a completed/attempted `contract_update`
    handoff *and* a `repair_action` projection at the same time.

    `contract_update` and `repair_action` must not both be present in the
    same preflight result. When both are given this returns a fail-closed
    verdict (`failure_code=multiple_mutation_intents`,
    `mutation_outcome=not_attempted`) *before* any GitHub mutation is
    attempted. A subsequent intent must be regenerated as a fresh preflight
    in a separate command, never resolved by this arbiter picking one of
    the two present intents.
    """
    has_contract_update = contract_update is not None
    has_repair_action = repair_action is not None

    if has_contract_update and has_repair_action:
        return {
            "intent": None,
            "ok": False,
            "failure_code": REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS,
            "mutation_outcome": "not_attempted",
            "reason": (
                "contract_update and repair_action are both present in the "
                "same preflight result; exactly one mutation intent is required. "
                "Apply one intent, then regenerate a fresh preflight as a "
                "separate command for the other."
            ),
        }
    if has_repair_action:
        return {
            "intent": "repair_action",
            "ok": True,
            "failure_code": None,
            "mutation_outcome": None,
            "reason": None,
        }
    if has_contract_update:
        return {
            "intent": "contract_update",
            "ok": True,
            "failure_code": None,
            "mutation_outcome": None,
            "reason": None,
        }
    return {
        "intent": None,
        "ok": False,
        "failure_code": REPAIR_APPLY_FAILURE_NO_MUTATION_INTENT,
        "mutation_outcome": "not_attempted",
        "reason": "Neither contract_update nor repair_action is present.",
    }


REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION = "repair_apply_result/v1"
EDIT_ISSUE_TXN_SCRIPT_REL = "edit-issue/scripts/edit_issue_txn.py"

# Issue #2039 AC3: provenance-mismatch failure codes, in the order they are
# checked (repo/issue identity first, then run identity, then payload
# replacement, then leaf-artifact digest).
REPAIR_APPLY_FAILURE_CROSS_ISSUE = "cross_issue_provenance_mismatch"
REPAIR_APPLY_FAILURE_STALE_RUN = "stale_run_provenance_mismatch"
REPAIR_APPLY_FAILURE_REPLACEMENT = "replacement_provenance_mismatch"
REPAIR_APPLY_FAILURE_DIGEST_MISMATCH = "digest_mismatch"
REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE = "provenance_unreconstructable"
REPAIR_APPLY_FAILURE_SECOND_DRIFT = "second_body_drift"
REPAIR_APPLY_FAILURE_NON_SAFE_AFTER_RERUN = "non_safe_disposition_after_rerun"


def _repair_apply_not_attempted_result(
    *,
    repo: str,
    issue_number: int,
    phase: str,
    failure_code: "str | None",
    provenance: "dict | None" = None,
    rebase: "dict | None" = None,
) -> dict:
    """Issue #2039 AC8/AC11: shared not_attempted stdout-contract shape for
    every early-exit branch of run_repair_action_apply() (multiple intent,
    no intent, non-safe disposition, unreadable candidate, provenance
    mismatch, second body drift, readback failure). Never emits a GitHub
    mutation."""
    return {
        "schema_version": REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION,
        "phase": phase,
        "mutation_outcome": "not_attempted",
        "failure_code": failure_code,
        "repo": repo,
        "issue_number": issue_number,
        "provenance": provenance
        or {
            "repo": repo,
            "issue_number": issue_number,
            "original_body_sha256": "",
            "original_updated_at": None,
            "preflight_run_identity": None,
            "producer_schema_version": "repair_action/v1",
            "producer_policy_version": "deterministic-issue-repair/v1",
            "repair_action_core_sha256": "",
            "candidate_digest": "",
            "source_lane": "unanchored",
            "source_refs_digest": None,
        },
        "rebase": rebase
        or {
            "attempted": False,
            "producer_reruns": 0,
            "drift_detected": False,
            "second_drift": False,
        },
        "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
        "receipt": {
            "patch_attempted": False,
            "executor_status": None,
            "mutation_outcome": "not_attempted",
            "failure_code": failure_code,
            "final_readback": {"status": "not_applicable", "digest": None, "digest_class": "not_applicable"},
        },
        "fresh_validation": {
            "status": "not_run",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        },
        "historical_artifacts": {"physically_deleted": False, "latest_action_reference_invalidated": False},
    }


def _repair_apply_strip_sha_prefix(value: "str | None") -> "str | None":
    """Normalize a `sha256:<hex>` or bare-`<hex>` digest string to its bare
    lowercase hex form for equality comparison, so digests produced by
    different callers (some prefixed, some not) can still be compared
    correctly (Issue #2039 AC3/AC4/AC5)."""
    if not value:
        return None
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value.lower()


def _repair_action_core_projection(repair_action: dict) -> dict:
    """Issue #2039 AC3: the environment-independent core of a repair_action
    payload that provenance binding is computed over."""
    return {
        "schema_version": repair_action.get("schema_version"),
        "policy_version": repair_action.get("policy_version"),
        "disposition": repair_action.get("disposition"),
        "original_body_sha256": repair_action.get("original_body_sha256"),
        "repaired_body_sha256": repair_action.get("repaired_body_sha256"),
        "repair_kinds": repair_action.get("repair_kinds"),
        "reason_codes": repair_action.get("reason_codes"),
    }


def _repair_action_core_sha256(repair_action: dict) -> str:
    core = _repair_action_core_projection(repair_action)
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _validate_repair_apply_provenance_binding(
    provenance: dict,
    *,
    repo: str,
    issue_number: int,
    expected_provenance: "dict | None",
) -> "str | None":
    """Issue #2039 AC3: reject cross-Issue, old-run, replacement, and
    candidate-digest-mismatch cases *before* any GitHub mutation is
    dispatched.

    The always-available self-consistency check (the candidate's own
    provenance must target the repo/issue this command was invoked for) is
    enforced unconditionally. When a caller additionally supplies
    `expected_provenance` (e.g. the run identity / core hash / candidate
    digest it originally bound to), every supplied field must match exactly,
    or this returns the corresponding fail-closed reason code. Returns None
    when provenance is fully consistent.
    """
    if provenance.get("repo") != repo or provenance.get("issue_number") != issue_number:
        return REPAIR_APPLY_FAILURE_CROSS_ISSUE
    if not expected_provenance:
        return None

    exp_repo = expected_provenance.get("repo")
    if exp_repo is not None and exp_repo != provenance.get("repo"):
        return REPAIR_APPLY_FAILURE_CROSS_ISSUE
    exp_issue = expected_provenance.get("issue_number")
    if exp_issue is not None and exp_issue != provenance.get("issue_number"):
        return REPAIR_APPLY_FAILURE_CROSS_ISSUE

    exp_run_identity = expected_provenance.get("preflight_run_identity")
    if exp_run_identity is not None and exp_run_identity != provenance.get("preflight_run_identity"):
        return REPAIR_APPLY_FAILURE_STALE_RUN
    exp_original_sha = expected_provenance.get("original_body_sha256")
    if exp_original_sha is not None and _repair_apply_strip_sha_prefix(
        exp_original_sha
    ) != _repair_apply_strip_sha_prefix(provenance.get("original_body_sha256")):
        return REPAIR_APPLY_FAILURE_STALE_RUN

    exp_core = expected_provenance.get("repair_action_core_sha256")
    if exp_core is not None and exp_core != provenance.get("repair_action_core_sha256"):
        return REPAIR_APPLY_FAILURE_REPLACEMENT

    exp_digest = expected_provenance.get("candidate_digest")
    if exp_digest is not None and _repair_apply_strip_sha_prefix(exp_digest) != _repair_apply_strip_sha_prefix(
        provenance.get("candidate_digest")
    ):
        return REPAIR_APPLY_FAILURE_DIGEST_MISMATCH

    return None


def _default_rerun_repair_producer(body: str, artifact_dir: Path) -> "tuple[dict | None, str | None]":
    """Issue #2039 AC4 default pre-dispatch rebase: rerun the SAME producer
    (repair_issue_contract.py dry-run, then --apply materialize) against a
    fresh live body -- never a whole-body textual rebase/diff-apply. Returns
    a repair_action-shaped dict (possibly non-`auto_apply_safe`, which the
    caller must classify as `non_safe_disposition_after_rerun`), or
    (None, reason) on any subprocess/materialization failure."""
    dry = _invoke_repair(body)
    if dry.get("error"):
        return None, f"rerun_dry_run_error:{dry.get('error')}"
    raw_action = dry.get("repair_action")
    if dry.get("changed") is not True or not isinstance(raw_action, dict):
        # No repair classified against the fresh body at all: there is
        # nothing safe left to apply, and the caller must not silently
        # fabricate a disposition here.
        return None, "rerun_no_actionable_repair"

    if raw_action.get("disposition") != "auto_apply_safe":
        return {
            "schema_version": raw_action.get("schema_version", "repair_action/v1"),
            "policy_version": raw_action.get("policy_version", "deterministic-issue-repair/v1"),
            "disposition": raw_action.get("disposition"),
            "original_body_sha256": raw_action.get("original_body_sha256"),
            "repaired_body_sha256": raw_action.get("repaired_body_sha256"),
            "candidate_body_artifact": None,
            "repair_kinds": raw_action.get("repair_kinds", []),
            "reason_codes": raw_action.get("reason_codes", []),
        }, None

    apply_result, apply_error = _materialize_auto_apply_candidate(body, dry, artifact_dir)
    if apply_error is not None:
        return None, f"rerun_apply_error:{apply_error}"
    candidate_path = artifact_dir / "repaired_issue_body.md"
    return {
        "schema_version": raw_action.get("schema_version", "repair_action/v1"),
        "policy_version": raw_action.get("policy_version", "deterministic-issue-repair/v1"),
        "disposition": "auto_apply_safe",
        "original_body_sha256": raw_action.get("original_body_sha256"),
        "repaired_body_sha256": raw_action.get("repaired_body_sha256"),
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": raw_action.get("repair_kinds", []),
        "reason_codes": raw_action.get("reason_codes", []),
    }, None


def _classify_repair_apply_readback_digest(
    remote_sha: "str | None", candidate_digest: "str | None", old_digest: "str | None"
) -> str:
    """Issue #2039 AC5: authoritative-readback digest classification.
    Distinguishes candidate (the dispatched body took effect) / old (the
    body is unchanged from before dispatch) / third (neither -- e.g. a
    concurrent human edit) / unknown (no readback digest available)."""
    norm_remote = _repair_apply_strip_sha_prefix(remote_sha)
    if norm_remote is None:
        return "unknown"
    if norm_remote == _repair_apply_strip_sha_prefix(candidate_digest):
        return "candidate"
    if old_digest is not None and norm_remote == _repair_apply_strip_sha_prefix(old_digest):
        return "old"
    return "third"


def _repair_receipt_from_txn_result(
    txn_result: dict,
    *,
    candidate_digest: "str | None" = None,
    old_digest: "str | None" = None,
    resolve_readback=None,
) -> dict:
    """Issue #2039 AC6: lossless projection of edit_issue_txn.py's
    ISSUE_EDIT_TXN_RESULT_V1 receipt into the repair_apply_result/v1
    receipt shape. `unknown` is preserved, never collapsed into
    failed/not_attempted/no_change.

    Issue #2039 AC5: post-dispatch retry budget is 0 -- this NEVER re-issues
    the mutation. When the transaction result carries no
    `remote_current_body_sha256` (executor could not itself confirm the
    outcome), `resolve_readback` (if given) is called at most once to fetch
    an authoritative post-dispatch snapshot for digest classification only;
    it never re-dispatches a mutation.
    """
    # Issue #2039 P0-4 fix: ISSUE_EDIT_TXN_RESULT_V1's canonical shape nests
    # the attempt/outcome/digest fields the receipt needs under
    # `body_update` and `content_update` (see edit_issue_txn.py
    # `_render_result()`); only `status` and `mutation_started` live at the
    # top level. Reading e.g. `txn_result["body_attempted"]` or
    # `txn_result["remote_current_body_sha256"]` at the top level always
    # misses (those keys never exist there), which previously caused
    # `patch_attempted` to silently degrade to False and skip fresh
    # validation even when a body patch had genuinely been attempted.
    body_update = txn_result.get("body_update")
    if not isinstance(body_update, dict):
        body_update = {}
    content_update = txn_result.get("content_update")
    if not isinstance(content_update, dict):
        content_update = {}

    status = txn_result.get("status")
    mutation_outcome_map = {
        "ok": "applied",
        "no_change": "no_change",
        "failed_no_mutation": "not_attempted",
        "human_judgment": "not_attempted",
        "mutation_outcome_unknown": "unknown",
        "failed_after_mutation": "unknown",
    }
    mutation_outcome = mutation_outcome_map.get(status, "unknown")
    patch_attempted = bool(
        body_update.get("attempted")
        or content_update.get("patch_attempted")
        or txn_result.get("mutation_started")
    )

    # Cross-field consistency (Issue #2039 P0-4 item 3): when a body/content
    # patch was actually attempted, the nested
    # `content_update.mutation_outcome` is the executor's own scoped signal
    # for that patch and MUST NOT be silently overridden by a more
    # optimistic top-level `status`-derived reading. If the executor itself
    # could not confirm the patch outcome
    # (`content_update.mutation_outcome == "unknown"`), this receipt's
    # `mutation_outcome` stays `unknown` even if the overall transaction
    # `status` looked definitive -- this is the exact
    # mutation_outcome_unknown hazard the review flagged (fail closed,
    # never collapse `unknown` into a definitive outcome).
    if patch_attempted and content_update.get("mutation_outcome") == "unknown":
        mutation_outcome = "unknown"

    remote_sha = body_update.get("remote_current_body_sha256")
    # PR #2202 review fix-delta (P1-3): a genuinely-unconfirmed outcome is
    # not only `mutation_outcome == "unknown"` -- an `applied` outcome with
    # no reported remote digest is equally unconfirmed (the executor
    # reported success but this receipt has no independent evidence of the
    # resulting body state). Attempt the SAME single authoritative readback
    # (never a mutation retry, still budget-1 per AC5) for that case too,
    # so `applied` never reaches `phase=complete` with an `unresolved`
    # final_readback (the exact schema invariant this fix-delta adds).
    if not remote_sha and mutation_outcome in ("unknown", "applied") and resolve_readback is not None:
        try:
            remote_sha = resolve_readback()
        except Exception:
            remote_sha = None

    if remote_sha:
        digest_class = _classify_repair_apply_readback_digest(remote_sha, candidate_digest, old_digest)
        final_readback = {"status": "verified", "digest": remote_sha, "digest_class": digest_class}
    else:
        final_readback = {"status": "unresolved", "digest": None, "digest_class": "not_applicable"}
        if mutation_outcome == "applied":
            # PR #2202 review fix-delta (P1-3): even after the readback
            # attempt above, no digest could be confirmed for a reported
            # `applied` outcome -- downgrade to `unknown` rather than
            # emitting a definitive `applied` result with no verifying
            # evidence. Mirrors the existing `mutation_outcome_unknown`
            # handling exactly (same failure_code, same AC5 single-read
            # budget already spent above).
            mutation_outcome = "unknown"

    failure_code = None
    if mutation_outcome == "unknown":
        failure_code = "final_readback_unresolvable"
    elif mutation_outcome == "not_attempted" and (txn_result.get("errors") or []):
        failure_code = "transaction_execute_error"

    return {
        "patch_attempted": patch_attempted,
        "executor_status": status,
        "mutation_outcome": mutation_outcome,
        "failure_code": failure_code,
        "final_readback": final_readback,
    }


REPAIR_APPLY_VALID_SOURCE_LANES = {"human_context", "anchor", "unanchored"}


def _default_fresh_validate_producer(
    body: str,
    expected_source_lane: str,
    *,
    anchor_url: "str | None" = None,
    known_context: "dict | None" = None,
) -> dict:
    """Issue #2039 AC9 default fresh-validation producer.

    Reruns the SAME narrow repair producer (dry-run only -- no
    materialization, no mutation) against `body` to determine whether an
    actionable repair still remains after the mutation attempt.

    PR #2202 review fix-delta (P0-5, item 4): when the caller supplies the
    SAME `anchor_url` / `known_context` inputs the original producer run
    used to classify its source lane, this now genuinely re-derives the
    lane by rerunning the SAME classifier
    (`_determine_repair_source_lane` -> `_resolve_scope_delta_source_kind`)
    against the CURRENT (fresh) state -- it does not simply trust/echo
    `expected_source_lane`. This is what makes a real lane-promotion /
    lane-mix-up bug detectable, not merely an injected test double. When no
    anchor context is supplied (the common case for callers that do not
    thread it through -- e.g. this function has no independent way to
    discover which GitHub comment was originally the anchor without being
    told), this stays the fail-closed-safe baseline of reporting the lane
    it was told to preserve (it never promotes unanchored -> anchor, never
    converts to with_anchor, and never mixes lanes on its own).
    """
    dry = _invoke_repair(body)
    if dry.get("error"):
        return {"error": dry.get("error"), "source_lane": expected_source_lane, "actionable_repair": None}
    raw_action = dry.get("repair_action")
    actionable = bool(
        dry.get("changed") is True
        and isinstance(raw_action, dict)
        and raw_action.get("disposition") == "auto_apply_safe"
    )
    if anchor_url is not None or known_context is not None:
        reported_lane = _determine_repair_source_lane(anchor_url, known_context)
    else:
        reported_lane = expected_source_lane
    return {"error": None, "source_lane": reported_lane, "actionable_repair": actionable}


def _repair_apply_expected_post_mutation_digest(
    *,
    mutation_outcome: str,
    receipt: dict,
    candidate_digest: "str | None",
    old_digest: "str | None",
) -> "str | None":
    """Issue #2039 AC9: the digest fresh validation must confirm the live
    body against, derived from the SAME authoritative signals AC5/AC6
    already computed -- never re-guessed independently.

    `applied` expects the candidate digest; `no_change` expects the
    pre-dispatch (old) digest; an `unknown` outcome defers entirely to the
    AC5 authoritative-readback digest classification (candidate/old), and
    is unresolvable (None) when that classification itself is `third` or
    `unknown` -- fresh validation must not fabricate an expectation the
    readback itself could not establish.
    """
    if mutation_outcome == "applied":
        return candidate_digest
    if mutation_outcome == "no_change":
        return old_digest
    digest_class = (receipt.get("final_readback") or {}).get("digest_class")
    if digest_class == "candidate":
        return candidate_digest
    if digest_class == "old":
        return old_digest
    return None


def _run_repair_apply_fresh_validation(
    *,
    fetch,
    producer,
    expected_source_lane: str,
    expected_digest: "str | None",
) -> dict:
    """Issue #2039 AC9: post-mutation fresh validation.

    Re-reads the live Issue body (a read, never a mutation retry -- this is
    independent of, and does not consume, the AC5 post-dispatch retry
    budget) and reruns `producer` against it to determine whether the
    original human-context/anchor/unanchored provenance lane was preserved
    and whether any actionable repair remains. Succeeds ONLY when the fresh
    result carries no actionable repair AND the live body digest matches
    `expected_digest`; an unresolvable expected digest (e.g. an `unknown`
    outcome whose readback digest_class was itself `third`/`unknown`) is
    always a failure, never a silent skip.
    """
    if expected_digest is None:
        return {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        }

    try:
        fresh_issue = fetch()
    except Exception:
        return {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        }

    fresh_body = fresh_issue.get("body", "") or ""
    fresh_digest = _sha256(fresh_body)
    digest_match = _repair_apply_strip_sha_prefix(fresh_digest) == _repair_apply_strip_sha_prefix(expected_digest)

    produced = producer(fresh_body)
    if produced.get("error") is not None:
        return {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": digest_match,
        }

    reported_lane = produced.get("source_lane")
    lane_preserved = reported_lane in REPAIR_APPLY_VALID_SOURCE_LANES and reported_lane == expected_source_lane
    actionable_remaining = produced.get("actionable_repair")

    status = "success" if (lane_preserved and actionable_remaining is False and digest_match) else "failed"
    return {
        "status": status,
        "source_lane_preserved": lane_preserved,
        "actionable_repair_remaining": actionable_remaining,
        "final_body_digest_match": digest_match,
    }


def _evaluate_candidate_static_readiness(
    *,
    candidate_body: str,
    candidate_path: Path,
) -> tuple[dict, "int | None"]:
    """Run the one static candidate-readiness evaluation for a dispatch.

    The generic lane retains its existing shared-core evaluation.  The
    structural lane evaluates its synthesized body before routing and passes
    this digest-bound result back into that same core, so a `go` candidate
    never launches a second checker subprocess.
    """
    readiness_script = _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
    returncode: "int | None" = None
    try:
        completed_readiness = subprocess.run(
            [sys.executable, str(readiness_script), "--body-file", str(candidate_path), "--mode", "static"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS,
        )
        readiness_stdout = completed_readiness.stdout
        returncode = completed_readiness.returncode
    except (subprocess.TimeoutExpired, OSError):
        # Static readiness runs strictly before edit_issue_txn.py.  Preserve
        # the established normalized input/runtime outcome and never imply
        # that a transaction was attempted.
        readiness_stdout = ""
    try:
        observed_readiness = json.loads(readiness_stdout)
    except json.JSONDecodeError:
        observed_readiness = {}
    if not isinstance(observed_readiness, dict):
        observed_readiness = {}

    # Keep the generic lane's historical degraded normalization unchanged,
    # while retaining the unmodified parsed envelope for the structural
    # lane's digest-bound validation below.  In particular, the structural
    # caller must be able to distinguish an absent checker digest from a
    # digest inserted by this legacy generic normalization.
    readiness = dict(observed_readiness)
    readiness.setdefault("status", "input_or_runtime_error")
    readiness.setdefault("body_sha256", f"sha256:{_sha256(candidate_body)}")
    readiness.setdefault("source_checks", [])
    readiness.setdefault("errors", [])
    readiness["_observed_checker_envelope"] = observed_readiness
    return readiness, returncode


def _dispatch_candidate_body_via_edit_txn(
    *,
    current_issue_: dict,
    candidate_body: str,
    candidate_path: Path,
    input_path: Path,
    scratch_dir_fd: int,
    root: Path,
    issue_number: int,
    repo: str,
    precomputed_readiness: "dict | None" = None,
) -> dict:
    """Issue #2039 AC8/AC11 (extracted for Issue #2396 AC5 sharing): the ONE
    mutation-dispatch core BOTH `repair_action.apply` (generic
    `run_repair_action_apply()`) and `structural_repair_action.apply`
    (`run_structural_repair_action_apply()`) invoke -- never a second,
    independent GitHub-mutation implementation for the structural lane.

    Builds the `edit_issue_txn.py` transaction input via
    `build_issue_edit_txn_input()`, dispatches it through the ONE
    GitHub-mutation subprocess this core ever invokes
    (`edit_issue_txn.py --input-file`, never a raw `gh issue edit` call --
    AC11), and losslessly surfaces a genuinely-unconfirmed
    (TimeoutExpired/OSError/non-JSON-stdout) outcome as
    `mutation_outcome_unknown` rather than a degraded `failed_no_mutation`
    (PR #2202 review fix-delta P0-6) -- fail-closed the SAME way for
    either caller lane.
    """
    from scope_signal_delta import build_issue_edit_txn_input

    # Generic callers leave `precomputed_readiness` unset and retain the
    # historical shared-core evaluation. Structural callers supply their
    # post-synthesis, candidate-digest-bound result so this core only forwards
    # it and never re-runs the checker.
    if precomputed_readiness is None:
        readiness, _ = _evaluate_candidate_static_readiness(
            candidate_body=candidate_body,
            candidate_path=candidate_path,
        )
    else:
        readiness = dict(precomputed_readiness)
    readiness["readiness_result_ref"] = "transaction-local"
    readiness_forwarding = {
        key: readiness[key]
        for key in (
            "status",
            "body_sha256",
            "source_checks",
            "errors",
            "readiness_result_ref",
            "resolution_evidence",
        )
        if key in readiness
    }

    transaction_input = build_issue_edit_txn_input(
        issue_number=issue_number,
        repo=repo,
        previous_body_sha256=f"sha256:{_sha256(current_issue_.get('body', ''))}",
        previous_updated_at=current_issue_["updatedAt"],
        new_body_file=str(candidate_path.relative_to(root)),
        readiness_result=readiness_forwarding,
    )
    # PR #2202 review fix (P1-1): dir-FD-relative O_CREAT|O_EXCL create
    # (see `_dispatch_candidate_body_default_transaction()`'s docstring
    # comment for the full rationale) instead of a plain
    # `Path.write_text()` at a fixed, guessable pathname.
    _repair_apply_scratch_create(
        scratch_dir_fd, input_path.name, json.dumps(transaction_input, ensure_ascii=False).encode("utf-8")
    )

    def _unknown_dispatch_txn_result(error_code: str, message: str) -> dict:
        # PR #2202 review fix-delta (P0-6): a genuinely-unconfirmed
        # outcome detected HERE (never through a raw-executor receipt)
        # still followed an actual edit_issue_txn.py subprocess
        # invocation attempt, so `content_update.patch_attempted=True`
        # is set (mirroring the canonical ISSUE_EDIT_TXN_RESULT_V1
        # `mutation_outcome_unknown` shape) so AC9 fresh validation
        # still runs after this outcome, exactly as it would for a
        # genuine executor-reported unknown receipt.
        return {
            "status": "mutation_outcome_unknown",
            "content_update": {"patch_attempted": True, "mutation_outcome": "unknown"},
            "errors": [{"code": error_code, "message": message[:200]}],
        }

    # AC11: the ONLY GitHub-mutation subprocess this consumer ever
    # invokes is edit_issue_txn.py --input-file. No `gh issue edit`
    # call exists anywhere in this function.
    transaction_script = _SCRIPTS_DIR.parent.parent / EDIT_ISSUE_TXN_SCRIPT_REL
    try:
        completed = subprocess.run(
            [sys.executable, str(transaction_script), "--input-file", str(input_path.relative_to(root))],
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(root),
            timeout=REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        # PR #2202 review fix-delta (P0-6, items 2+3): this is the ONLY
        # subprocess this consumer ever invokes to dispatch a real
        # GitHub PATCH (AC11). `subprocess.run(timeout=...)` kills the
        # child only AFTER the timeout has already elapsed, so a
        # TimeoutExpired here does NOT prove the PATCH never reached
        # GitHub -- the child (or GitHub itself) may already have
        # applied it. Reporting this as "no mutation" would be an
        # unsafe mis-report of a non-retriable PATCH (review P0-6 item
        # 3), so this is always routed to the SAME
        # `mutation_outcome_unknown` status the executor itself uses to
        # signal an unconfirmed outcome -- never a degraded
        # `failed_no_mutation` shortcut. `_repair_receipt_from_txn_result`
        # maps this to `mutation_outcome=unknown` and triggers the
        # existing AC5 single authoritative-readback path.
        return _unknown_dispatch_txn_result("txn_subprocess_timeout", str(exc))
    except OSError as exc:
        # Same reasoning as the TimeoutExpired branch above: an OSError
        # raised by subprocess.run() itself (e.g. a transient OS-level
        # wait/communicate failure) after the call has already been
        # made does not prove the PATCH was never dispatched.
        return _unknown_dispatch_txn_result("txn_subprocess_oserror", str(exc))
    try:
        txn_result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        # PR #2202 review fix-delta (P0-6, item 3): stdout being empty,
        # truncated, or non-JSON AFTER the subprocess call above has
        # already completed does NOT prove no mutation happened -- the
        # child may have dispatched the PATCH and then failed to
        # flush/emit its own confirmation. Degrading this to
        # `failed_no_mutation` (the prior behavior) risked silently
        # reporting "no mutation" after a real, non-retriable PATCH.
        # This must resolve to `unknown` and route into the SAME AC5
        # authoritative-readback path used for a genuine executor-
        # reported `mutation_outcome_unknown` receipt.
        txn_result = _unknown_dispatch_txn_result("txn_stdout_not_json", completed.stderr or "")
    # Same reasoning: a parsed-but-non-dict stdout shape still followed
    # a completed subprocess call, so it must not be reported as
    # `failed_no_mutation` either.
    return txn_result if isinstance(txn_result, dict) else _unknown_dispatch_txn_result(
        "txn_stdout_not_dict", "parsed stdout JSON was not a dict"
    )


def _dispatch_candidate_body_default_transaction(
    current_issue_: dict,
    candidate_body: str,
    *,
    root: Path,
    issue_number: int,
    repo: str,
    scratch_subdir: str = "repair-action-apply",
    precomputed_readiness: "dict | None" = None,
) -> dict:
    """Issue #2039 AC8 (extracted for Issue #2396 AC5 sharing): unlike the
    sibling contract_update.run.with_anchor lane (which uses the shared,
    non-Issue-scoped `tmp/` workspace), this lane's transaction-local
    scratch files live under this command_id's own declared
    `allowed_write_roots` entry
    (`.claude/artifacts/issue-refinement-loop/{issue_number}/{scratch_subdir}/`),
    so they are never reported as an unauthorized write by
    skill_runtime_exec.py's git-status-diff postcondition check (which has
    no `tmp/`-specific exemption of its own for a bare ignored directory's
    mere existence). `scratch_subdir` keeps the generic `repair_action.apply`
    lane's scratch files and the structural `structural_repair_action.apply`
    lane's scratch files in separate subdirectories so two concurrent
    invocations for the SAME Issue across the two lanes can never collide
    on each other's filenames.

    PR #2202 review fix (P1-1): a FIXED issue-number-keyed scratch path
    here (e.g. `issue_<N>_repair_action_candidate.md`) lets two concurrent
    invocations for the same Issue clobber each other's candidate/input
    files, and -- worse -- lets another process with the same permissions
    pre-place a symlink at that exact, guessable path BEFORE this code
    ever runs, redirecting the write below to silently overwrite an
    arbitrary writable file elsewhere. This happens strictly upstream of
    `edit_issue_txn.py`'s own symlink checks, which only ever see the
    file we already (unsafely) wrote. Use a per-invocation random
    subdirectory (`tempfile.mkdtemp`, mode 0700, unguessable name) under
    the same allowed-write-roots base, then create each scratch file
    inside it via dir-FD-relative `O_CREAT|O_EXCL|O_NOFOLLOW` so we fail
    closed instead of following/overwriting anything unexpectedly already
    there.
    """
    _txn_base_dir = root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number) / scratch_subdir
    _txn_base_dir.mkdir(parents=True, exist_ok=True)
    _scratch_dir = Path(tempfile.mkdtemp(dir=str(_txn_base_dir)))
    candidate_path = _scratch_dir / "candidate_body.md"
    input_path = _scratch_dir / "edit_issue_txn_input.json"

    _scratch_dir_fd = os.open(str(_scratch_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _repair_apply_scratch_create(_scratch_dir_fd, candidate_path.name, candidate_body.encode("utf-8"))
        try:
            return _dispatch_candidate_body_via_edit_txn(
                current_issue_=current_issue_,
                candidate_body=candidate_body,
                candidate_path=candidate_path,
                input_path=input_path,
                scratch_dir_fd=_scratch_dir_fd,
                root=root,
                issue_number=issue_number,
                repo=repo,
                precomputed_readiness=precomputed_readiness,
            )
        finally:
            for _tmp_path in (candidate_path, input_path):
                _repair_apply_scratch_unlink(_scratch_dir_fd, _tmp_path.name)
    finally:
        with contextlib.suppress(OSError):
            os.close(_scratch_dir_fd)
        with contextlib.suppress(OSError):
            os.rmdir(_scratch_dir)


def run_repair_action_apply(
    *,
    repo: str,
    issue_number: int,
    preflight_result_path: str,
    repo_root: "Path | None" = None,
    fetch_current=None,
    apply_transaction=None,
    expected_provenance: "dict | None" = None,
    rerun_producer=None,
    fresh_validate=None,
    fresh_anchor_url: "str | None" = None,
    fresh_known_context: "dict | None" = None,
) -> dict:
    """Issue #2039 AC8/AC11: `repair_action.apply` controlled consumer.

    Reads a previously-produced preflight result artifact via the FD-based
    secure reader (AC7), resolves the exactly-one mutation intent (AC1),
    and -- only when the intent is `repair_action` with
    `disposition == auto_apply_safe` -- dispatches the repaired body
    through the existing `edit_issue_txn.py` controlled transaction script
    (a real subprocess call; never a raw `gh issue edit` call -- AC11).

    AC3 (provenance binding): the candidate's own repo/issue_number is
    always cross-checked against the requested target. When a caller
    supplies `expected_provenance` (repo, issue_number,
    preflight_run_identity, original_body_sha256, repair_action_core_sha256,
    candidate_digest), every supplied field is required to match exactly, or
    dispatch is rejected before any GitHub mutation
    (cross_issue_provenance_mismatch / stale_run_provenance_mismatch /
    replacement_provenance_mismatch / digest_mismatch).

    AC4 (body-drift handling): before dispatch, the live Issue body is
    compared against the candidate's own recorded `original_body_sha256`.
    On drift, the producer (repair_issue_contract.py) is rerun at most once
    against the fresh live body (never a whole-body textual rebase/diff
    apply) via `rerun_producer` (injectable; defaults to
    `_default_rerun_repair_producer`). A second drift, a non-safe
    disposition after rerun, or an unreconstructable rerun result fails
    closed with no mutation.

    AC5 (retry-budget separation): `retry.post_dispatch_retry_budget` is
    always 0 -- this function NEVER re-dispatches `edit_issue_txn.py` after
    a first attempt. An executor-unconfirmed (`unknown`) outcome is instead
    resolved, at most once, via an authoritative readback (a read, not a
    mutation) that classifies the live digest as candidate/old/third.

    AC6 (lossless receipt projection): `edit_issue_txn.py`'s receipt fields
    are projected losslessly via `_repair_receipt_from_txn_result()`;
    `unknown` is never collapsed into failed/not_attempted/no_change, and
    `phase`/`mutation_outcome`/`failure_code` are kept as separate fields
    (the repair lane never emits `contract_patch_plan_missing`).

    AC9 (lane-preserving fresh validation): after any attempt that reaches
    transaction dispatch (`receipt.patch_attempted` is True -- applied,
    no_change, or unknown outcomes; never a not_attempted outcome, since
    nothing was ever attempted there), a single read-only fresh validation
    reruns `fresh_validate` (injectable; defaults to
    `_default_fresh_validate_producer`) against the now-current live body
    and re-reads the live Issue once more. It succeeds ONLY when the fresh
    result reports no actionable repair remaining, the original source
    lane (human_context/anchor/unanchored) is preserved (never an
    unconditional `with_anchor` conversion, source-lane promotion, or lane
    mix-up), and the live body digest matches the digest AC5's own
    authoritative-readback classification already established. This never
    re-dispatches a mutation and is independent of the AC5 post-dispatch
    retry budget. `fresh_anchor_url` / `fresh_known_context` (optional) are
    forwarded to the default fresh-validation producer so it can genuinely
    re-derive the source lane from current state instead of echoing back
    the lane it was told to preserve (PR #2202 review fix-delta, P0-5).

    PR #2202 review fix-delta (P0-5): a fresh validation `status ==
    "failed"` that occurs after a mutation that otherwise reached
    `phase == "complete"` (an `applied`/`no_change` outcome) now overrides
    `phase` to `"fresh_validation"` and sets a non-null `failure_code`
    (`source_lane_mismatch` when the lane was not preserved, else
    `fresh_validation_failed`) -- `mutation_outcome` itself is never
    altered (it is a fact about GitHub state, independent of this
    consistency check), but a genuine fresh-validation failure can no
    longer be silently reported as `phase=complete` /
    `failure_code=null`.

    AC10 (historical artifact immutability): this function never deletes
    the preflight-result artifact it reads, the producer's own recorded
    `candidate_body_artifact`, or any rebase-rerun artifact -- only its own
    transaction-local scratch re-serialization (a fresh copy written for
    `edit_issue_txn.py`'s own consumption) is ever removed, in a `finally`
    block, after dispatch. A replay against a live body that already
    matches this candidate's own recorded `repaired_body_sha256` (i.e. the
    change was already applied by a prior invocation) is detected before
    the AC4 rebase budget is spent and resolves deterministically to
    `mutation_outcome=no_change` with no GitHub mutation dispatched.
    """
    root = repo_root or _find_repo_root()
    _pf_path = Path(preflight_result_path)
    if not _pf_path.is_absolute():
        _pf_path = root / _pf_path
    try:
        result_text, _result_digest = secure_read_repair_apply_artifact(_pf_path, root=root)
    except RepairApplySecureOpenError:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )
    if not isinstance(parsed, dict):
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    contract_update = parsed.get("contract_update")
    repair_action = parsed.get("repair_action")
    intent = resolve_repair_apply_mutation_intent(
        contract_update=contract_update, repair_action=repair_action
    )
    if not intent["ok"]:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code=intent["failure_code"]
        )

    if not isinstance(repair_action, dict) or repair_action.get("disposition") != "auto_apply_safe":
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="provenance_validation", failure_code="invalid_disposition"
        )

    candidate_body_artifact = repair_action.get("candidate_body_artifact")
    if not candidate_body_artifact:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    raw_source_lane = repair_action.get("source_lane")
    source_lane = raw_source_lane if raw_source_lane in {"human_context", "anchor", "unanchored"} else "unanchored"

    repair_action_core_sha256 = _repair_action_core_sha256(repair_action)

    _candidate_path = Path(candidate_body_artifact)
    if not _candidate_path.is_absolute():
        _candidate_path = root / _candidate_path
    try:
        candidate_body_text, candidate_digest = secure_read_repair_apply_artifact(
            _candidate_path,
            root=root,
            expected_sha256=repair_action.get("repaired_body_sha256"),
        )
    except RepairApplySecureOpenError:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    # PR #2202 review fix (P0-2): these are nested under `repair_action.*`
    # (and `hashes.result_core_sha256`) in the canonical
    # refinement_preflight_result_v1 schema, never top-level `parsed.*`.
    # Reading the wrong location made every real production artifact report
    # null/None here (run identity + updatedAt), silently disabling the
    # provenance binding this AC exists to enforce.
    provenance = {
        "repo": repo,
        "issue_number": issue_number,
        "original_body_sha256": repair_action.get("original_body_sha256") or "",
        "original_updated_at": repair_action.get("original_updated_at"),
        "preflight_run_identity": (
            repair_action.get("preflight_run_identity")
            or (parsed.get("hashes") if isinstance(parsed.get("hashes"), dict) else {}).get("result_core_sha256")
        ),
        "producer_schema_version": repair_action.get("schema_version") or "repair_action/v1",
        "producer_policy_version": repair_action.get("policy_version") or "deterministic-issue-repair/v1",
        "repair_action_core_sha256": repair_action_core_sha256,
        "candidate_digest": candidate_digest,
        "source_lane": source_lane,
        "source_refs_digest": repair_action.get("source_refs_digest"),
    }
    if (
        provenance["producer_schema_version"] != "repair_action/v1"
        or provenance["producer_policy_version"] != "deterministic-issue-repair/v1"
        or not provenance["original_body_sha256"]
        or not provenance["repair_action_core_sha256"]
        or not provenance["candidate_digest"]
        # PR #2202 review fix-delta (P1-3): a null `original_updated_at` or
        # `preflight_run_identity` silently disables the P0-3 ABA (A->B->A)
        # protection (the pre-dispatch drift check falls back to a
        # body-SHA-only comparison whenever `original_updated_at` is None --
        # see the `updated_at_drift` computation below). Auto-apply
        # dispatch must never proceed on that degraded protection; only a
        # producer run that actually recorded both fields may reach
        # transaction_execute. This closes the gap fail-closed rather than
        # relying on the (undispatched) historical-artifact-replay comment.
        or provenance["original_updated_at"] is None
        or provenance["preflight_run_identity"] is None
    ):
        return _repair_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
            provenance=provenance,
        )

    # AC3: bind against repo/issue identity always, and against any
    # caller-declared expectation (run identity, core hash, candidate
    # digest) when supplied. Fail-closed BEFORE any GitHub mutation.
    _provenance_failure = _validate_repair_apply_provenance_binding(
        provenance, repo=repo, issue_number=issue_number, expected_provenance=expected_provenance
    )
    if _provenance_failure is not None:
        return _repair_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=_provenance_failure,
            provenance=provenance,
        )

    def _default_fetch_current():
        current_issue, issue_error = _fetch_issue(repo, issue_number)
        if current_issue is None:
            raise RuntimeError(f"issue_readback_failed:{issue_error}")
        return current_issue

    fetch = fetch_current or _default_fetch_current
    try:
        current_issue = fetch()
    except Exception:
        return _repair_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="precondition_read",
            failure_code="final_readback_unresolvable",
            provenance=provenance,
        )

    # AC4: pre-dispatch body-drift handling. Compare the live body against
    # the candidate's own recorded original_body_sha256. On drift, rerun the
    # producer AT MOST ONCE against the fresh live body (never a whole-body
    # textual rebase/diff-apply); a second drift, a non-safe disposition
    # after rerun, or an unreconstructable rerun result fails closed with no
    # mutation dispatched.
    rebase_projection = {"attempted": False, "producer_reruns": 0, "drift_detected": False, "second_drift": False}
    live_body = current_issue.get("body", "") or ""
    live_body_digest = hashlib.sha256(live_body.encode("utf-8")).hexdigest()
    live_updated_at = current_issue.get("updatedAt")
    # PR #2202 review fix (P0-3): body-SHA-only drift detection allows an
    # A(t1) -> B(t2) -> A(t3) ABA sequence through undetected (body SHA
    # matches again at t3, but the candidate was generated against the t1
    # authority epoch, not t3). When the producer recorded a non-null
    # original_updated_at (fresh, post-migration artifacts), ALSO compare it
    # against the live updatedAt; either mismatch counts as drift. Historical
    # pre-migration artifacts with a null original_updated_at fall back to
    # the pre-existing body-SHA-only comparison (they never recorded an
    # updatedAt to compare against).
    expected_updated_at = provenance.get("original_updated_at")
    updated_at_drift = expected_updated_at is not None and live_updated_at != expected_updated_at
    drift_detected = (
        _repair_apply_strip_sha_prefix(live_body_digest)
        != _repair_apply_strip_sha_prefix(repair_action.get("original_body_sha256"))
        or updated_at_drift
    )

    if drift_detected:
        rebase_projection["drift_detected"] = True

        # AC10: replay-of-an-already-applied-change short circuit. If the
        # live body already matches THIS candidate's own recorded target
        # (repaired_body_sha256), the requested change was already
        # achieved by a prior invocation (or externally) -- this is a
        # replay, not a fresh drift that needs rebasing. Resolve
        # deterministically to no_change WITHOUT spending the single
        # pre-dispatch rebase budget and WITHOUT dispatching any GitHub
        # mutation.
        if _repair_apply_strip_sha_prefix(live_body_digest) == _repair_apply_strip_sha_prefix(
            repair_action.get("repaired_body_sha256")
        ):
            return {
                "schema_version": REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION,
                "phase": "complete",
                "mutation_outcome": "no_change",
                "failure_code": None,
                "repo": repo,
                "issue_number": issue_number,
                "provenance": provenance,
                "rebase": rebase_projection,
                "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
                "receipt": {
                    "patch_attempted": False,
                    "executor_status": None,
                    "mutation_outcome": "no_change",
                    "failure_code": None,
                    "final_readback": {
                        "status": "verified",
                        "digest": f"sha256:{live_body_digest}",
                        "digest_class": "candidate",
                    },
                },
                "fresh_validation": {
                    "status": "not_run",
                    "source_lane_preserved": True,
                    "actionable_repair_remaining": None,
                    "final_body_digest_match": None,
                },
                "historical_artifacts": {"physically_deleted": False, "latest_action_reference_invalidated": False},
            }

        rebase_projection["attempted"] = True
        rebase_projection["producer_reruns"] = 1

        rebase_dir = (
            root
            / ".claude"
            / "artifacts"
            / "issue-refinement-loop"
            / str(issue_number)
            / "repair-action-apply"
            / "rebase"
        )
        rebase_dir.mkdir(parents=True, exist_ok=True)
        rerun_fn = rerun_producer or (lambda body: _default_rerun_repair_producer(body, rebase_dir))
        rerun_action, rerun_error = rerun_fn(live_body)

        if rerun_error is not None or not isinstance(rerun_action, dict):
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
                provenance=provenance,
                rebase=rebase_projection,
            )
        if rerun_action.get("disposition") != "auto_apply_safe" or not rerun_action.get("candidate_body_artifact"):
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_NON_SAFE_AFTER_RERUN,
                provenance=provenance,
                rebase=rebase_projection,
            )

        # Second live read: detect whether the body drifted again between
        # the rerun's basis (live_body) and now. If it did, fail closed --
        # the single rebase budget is exhausted (post-dispatch retry stays
        # separately budgeted at 0; this is still pre-dispatch).
        try:
            second_issue = fetch()
        except Exception:
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
                provenance=provenance,
                rebase=rebase_projection,
            )
        second_body = second_issue.get("body", "") or ""
        second_body_digest = hashlib.sha256(second_body.encode("utf-8")).hexdigest()
        second_updated_at = second_issue.get("updatedAt")
        # PR #2202 review fix (P0-3): the second (post-rerun) drift check
        # must ALSO catch an updatedAt-only change (body text happens to
        # match the rerun's basis again, but updatedAt advanced between the
        # rerun's basis read and this second read) -- not just a
        # body-digest change.
        if second_body_digest != live_body_digest or second_updated_at != live_updated_at:
            rebase_projection["second_drift"] = True
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_SECOND_DRIFT,
                provenance=provenance,
                rebase=rebase_projection,
            )

        # No second drift: the rerun's candidate is safe to dispatch.
        # Re-read the rerun candidate through the FD-based secure reader
        # (AC7) and rebuild provenance from the rerun repair_action, never
        # by textually patching the original candidate.
        rerun_candidate_path = Path(rerun_action["candidate_body_artifact"])
        if not rerun_candidate_path.is_absolute():
            rerun_candidate_path = root / rerun_candidate_path
        try:
            candidate_body_text, candidate_digest = secure_read_repair_apply_artifact(
                rerun_candidate_path,
                root=root,
                expected_sha256=rerun_action.get("repaired_body_sha256"),
            )
        except RepairApplySecureOpenError:
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
                provenance=provenance,
                rebase=rebase_projection,
            )

        repair_action = rerun_action
        current_issue = second_issue
        repair_action_core_sha256 = _repair_action_core_sha256(repair_action)
        # PR #2202 review fix (P0-3): after a rerun, re-bind to the NEW body
        # SHA, NEW updatedAt, a NEW run identity, and the new repair_action
        # core/candidate digests -- while source_lane and source_refs_digest
        # stay pinned to the values already in `provenance` (same underlying
        # human-context/anchor lane; only the body/candidate were
        # regenerated against the current authority epoch).
        provenance = {
            **provenance,
            "original_body_sha256": repair_action.get("original_body_sha256") or "",
            "original_updated_at": second_updated_at,
            "preflight_run_identity": "sha256:"
            + _sha256(
                _canonical_json(
                    {
                        "issue_number": issue_number,
                        "repo": repo,
                        "original_body_sha256": repair_action.get("original_body_sha256"),
                        "original_updated_at": second_updated_at,
                        "source_lane": provenance.get("source_lane"),
                        "rebase": True,
                    }
                )
            ),
            "repair_action_core_sha256": repair_action_core_sha256,
            "candidate_digest": candidate_digest,
        }

    def _default_apply_transaction(current_issue_: dict, candidate_body: str) -> dict:
        # Issue #2396: this default now delegates to the top-level, shared
        # `_dispatch_candidate_body_default_transaction()` helper (AC5 of
        # Issue #2396 -- the structural_repair_action.apply consumer reuses
        # the EXACT SAME mutation-dispatch core via this same helper, never
        # a second independent implementation). See that function's own
        # docstring for the full scratch-directory / symlink-safety
        # rationale (unchanged from before this extraction).
        return _dispatch_candidate_body_default_transaction(
            current_issue_, candidate_body, root=root, issue_number=issue_number, repo=repo
        )

    apply_fn = apply_transaction or _default_apply_transaction
    txn_result = apply_fn(current_issue, candidate_body_text)

    def _resolve_readback() -> "str | None":
        # AC5: a single authoritative read (never a mutation retry) used
        # only when the transaction result itself carried no
        # remote_current_body_sha256.
        fresh_issue = fetch()
        return f"sha256:{_sha256(fresh_issue.get('body', '') or '')}"

    receipt = _repair_receipt_from_txn_result(
        txn_result,
        candidate_digest=candidate_digest,
        old_digest=live_body_digest,
        resolve_readback=_resolve_readback,
    )
    mutation_outcome = receipt["mutation_outcome"]
    # AC6: phase/mutation_outcome/failure_code stay separate fields; an
    # `unknown` outcome (executor could not itself confirm the result) never
    # reaches phase=complete (schema-enforced invariant).
    phase = "complete" if mutation_outcome in {"applied", "no_change", "not_attempted"} else "final_readback"
    failure_code = receipt["failure_code"]

    # AC9: fresh validation only runs after an attempt that actually
    # reached transaction dispatch (patch_attempted) -- a not_attempted
    # outcome never touched GitHub, so there is nothing post-mutation to
    # validate, and fresh_validation stays not_run.
    if receipt.get("patch_attempted"):
        expected_digest = _repair_apply_expected_post_mutation_digest(
            mutation_outcome=mutation_outcome,
            receipt=receipt,
            candidate_digest=candidate_digest,
            old_digest=live_body_digest,
        )
        fresh_validate_fn = fresh_validate or (
            lambda body: _default_fresh_validate_producer(
                body, source_lane, anchor_url=fresh_anchor_url, known_context=fresh_known_context
            )
        )
        fresh_validation = _run_repair_apply_fresh_validation(
            fetch=fetch,
            producer=fresh_validate_fn,
            expected_source_lane=source_lane,
            expected_digest=expected_digest,
        )
    else:
        fresh_validation = {
            "status": "not_run",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        }

    # PR #2202 review fix-delta (P0-5): a fresh-validation failure that
    # follows an otherwise-`complete` phase (applied/no_change) must be
    # surfaced -- it must not be silently absorbed into
    # phase=complete/failure_code=null. `mutation_outcome` itself is left
    # untouched: it is a fact about GitHub state (the mutation genuinely
    # happened or did not), separate from this post-mutation consistency
    # check.
    if fresh_validation["status"] == "failed" and phase == "complete":
        phase = "fresh_validation"
        failure_code = (
            "source_lane_mismatch" if fresh_validation["source_lane_preserved"] is False else "fresh_validation_failed"
        )

    return {
        "schema_version": REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION,
        "phase": phase,
        "mutation_outcome": mutation_outcome,
        "failure_code": failure_code,
        "repo": repo,
        "issue_number": issue_number,
        "provenance": provenance,
        "rebase": rebase_projection,
        "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
        "receipt": receipt,
        "fresh_validation": fresh_validation,
        "historical_artifacts": {
            "physically_deleted": False,
            "latest_action_reference_invalidated": mutation_outcome == "applied",
        },
    }


# ---------------------------------------------------------------------------
# Issue #2396: `structural_repair_action.apply` controlled consumer.
#
# Bridges `repair_issue_contract.py`'s `build_structural_repair_bundle()`
# (Issue #995 -- a pure, mutation-free producer of per-field insertion
# proposals) to the SAME `edit_issue_txn.py` controlled-transaction core
# `run_repair_action_apply()` already uses. Unlike the generic
# `repair_action/v1` lane, `structural_repair_action/v1` never carries a
# pre-materialized whole-body `candidate_body_artifact` -- its `items[]`
# are per-field insertion metadata (`anchor_start_line` / `relation` /
# `candidate_value` / digests) that this consumer must independently
# re-verify against the LIVE Issue body and synthesize into a single
# repaired body BEFORE it can even be handed to the shared dispatch core.
# ---------------------------------------------------------------------------

STRUCTURAL_REPAIR_APPLY_STDOUT_SCHEMA_VERSION = "structural_repair_apply_result/v1"

STRUCTURAL_REPAIR_APPLY_FAILURE_INVALID_DISPOSITION = "invalid_disposition"
STRUCTURAL_REPAIR_APPLY_FAILURE_AMBIGUOUS_ITEM = "ambiguous_insertion_present"
STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH = "structural_item_digest_mismatch"
STRUCTURAL_REPAIR_APPLY_FAILURE_CONFLICTING_TARGETS = "structural_conflicting_insertion_targets"
STRUCTURAL_REPAIR_APPLY_FAILURE_BODY_DRIFT = "structural_body_drift"
STRUCTURAL_REPAIR_APPLY_FAILURE_CROSS_ISSUE = "cross_issue_provenance_mismatch"
STRUCTURAL_REPAIR_APPLY_FAILURE_READBACK_UNRESOLVABLE = "final_readback_unresolvable"
STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_NEEDS_FIX = "structural_readiness_needs_fix"
STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_HUMAN_JUDGMENT = "structural_readiness_human_judgment"
STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_RUNTIME_ERROR = "structural_readiness_runtime_error"
STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_INPUT_OR_RUNTIME_ERROR = "structural_readiness_input_or_runtime_error"

_STRUCTURAL_READINESS_EXPECTED_EXIT_CODES = {
    "go": 0,
    "needs_fix": 1,
    "human_judgment": 2,
    "runtime_error": 4,
}
_STRUCTURAL_READINESS_FAILURE_CODES = {
    "needs_fix": STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_NEEDS_FIX,
    "human_judgment": STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_HUMAN_JUDGMENT,
    "runtime_error": STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_RUNTIME_ERROR,
    "input_or_runtime_error": STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_INPUT_OR_RUNTIME_ERROR,
}


def _structural_readiness_diagnostics(readiness: dict) -> dict:
    """Project only bounded, deterministic readiness details to stdout."""
    rule_ids = sorted(
        {
            error.get("rule_id")
            for error in readiness.get("errors", [])
            if isinstance(error, dict) and isinstance(error.get("rule_id"), str)
        }
    )
    return {
        "status": readiness.get("status"),
        "rule_ids": rule_ids[:16],
        "truncated": len(rule_ids) > 16,
    }


def _structural_digest_bound_readiness(
    *, candidate_body: str, candidate_path: Path
) -> dict:
    """Evaluate one structural candidate at the strict digest-bound boundary.

    The generic consumer intentionally retains the readiness normalizations it
    has historically forwarded through the shared core.  Structural routing is
    different: it must validate the checker envelope actually observed before
    any normalized field can influence transaction dispatch.
    """
    readiness, returncode = _evaluate_candidate_static_readiness(
        candidate_body=candidate_body,
        candidate_path=candidate_path,
    )
    # Production evaluation retains the unmodified parsed checker object under
    # this private key.  Test doubles that supply the evaluator directly are
    # treated as the observed envelope, preserving their focused seam.
    observed = readiness.get("_observed_checker_envelope")
    if not isinstance(observed, dict):
        observed = readiness

    status = observed.get("status")
    candidate_digest = f"sha256:{_sha256(candidate_body)}"
    expected_exit_code = _STRUCTURAL_READINESS_EXPECTED_EXIT_CODES.get(status) if isinstance(status, str) else None
    if (
        not isinstance(status, str)
        or expected_exit_code is None
        or returncode != expected_exit_code
        or not isinstance(observed.get("body_sha256"), str)
        or observed["body_sha256"] != candidate_digest
        or not isinstance(observed.get("source_checks"), list)
        or not isinstance(observed.get("errors"), list)
    ):
        # Malformed process results, unknown status, exit/status disagreement,
        # incomplete envelopes, and digest mismatch all fail before the shared
        # transaction core.  Do not expose the raw checker payload.
        return {
            "status": "input_or_runtime_error",
            "source_checks": [],
            "errors": [],
        }

    # Forward only the validated envelope fields.  This keeps private
    # evaluator metadata and any unrelated checker output out of the
    # transaction input and stdout contract.
    return {
        "status": status,
        "body_sha256": observed["body_sha256"],
        "source_checks": observed["source_checks"],
        "errors": observed["errors"],
    }


def _structural_apply_not_attempted_result(
    *,
    repo: str,
    issue_number: int,
    phase: str,
    failure_code: "str | None",
    readiness_diagnostics: "dict | None" = None,
) -> dict:
    """Shared not_attempted stdout-contract shape for every early-exit
    branch of `run_structural_repair_action_apply()`. Never emits a GitHub
    mutation (mirrors `_repair_apply_not_attempted_result()`'s role for the
    sibling generic lane)."""
    result = {
        "schema_version": STRUCTURAL_REPAIR_APPLY_STDOUT_SCHEMA_VERSION,
        "phase": phase,
        "mutation_outcome": "not_attempted",
        "failure_code": failure_code,
        "repo": repo,
        "issue_number": issue_number,
        "items_applied": 0,
        "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
        "receipt": {
            "patch_attempted": False,
            "executor_status": None,
            "mutation_outcome": "not_attempted",
            "failure_code": failure_code,
            "final_readback": {"status": "not_applicable", "digest": None, "digest_class": "not_applicable"},
        },
        "fresh_validation": {
            "status": "not_run",
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        },
    }
    if readiness_diagnostics is not None:
        result["readiness_diagnostics"] = readiness_diagnostics
    return result


def _structural_item_rendered_block(item: dict) -> str:
    """Reproduce the EXACT same candidate-section text the producer hashed
    into `insertion.candidate_section_digest`
    (`repair_issue_contract._apply_insertion_decision`'s own formula) so
    this consumer's digest re-verification is a genuine byte-for-byte
    check, not an independently-invented format."""
    insertion = item["insertion"]
    rendered_heading = insertion["rendered_heading"]
    candidate_value = item.get("candidate_value")
    if candidate_value:
        return f"{rendered_heading}\n\n{candidate_value}\n"
    return f"{rendered_heading}\n"


def _structural_item_block_lines(item: dict) -> list[str]:
    """The rendered block as a list of body lines (no trailing empty
    component from the block's own trailing newline) suitable for splicing
    directly into a `body.split(\"\\n\")` line list."""
    text = _structural_item_rendered_block(item)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _verify_structural_items_against_live_body(items: list[dict], live_body: str) -> "str | None":
    """Issue #2396 AC5: re-verify EVERY item's own
    `insertion.candidate_section_digest` / `insertion.anchor_digest`
    against the freshly-fetched LIVE Issue body BEFORE any synthesis --
    never trusting the artifact's own recorded digests alone (defense in
    depth, mirroring this codebase's existing stance of never trusting a
    producer's summary field without independently re-checking its own
    items -- see `route_structural_repair_disposition()`).

    Returns None on success, or a failure_code string on the first
    mismatch / ambiguous item encountered.
    """
    from repair_issue_contract import _parse_h2_sections, _section_line_bounds

    live_lines = live_body.split("\n")
    sections = _parse_h2_sections(live_body)
    heading_index: dict[str, list[dict]] = {}
    for sec in sections:
        heading_index.setdefault(sec["heading"].strip().casefold(), []).append(sec)

    for item in items:
        insertion = item.get("insertion")
        if not isinstance(insertion, dict) or insertion.get("disposition") == "ambiguous":
            return STRUCTURAL_REPAIR_APPLY_FAILURE_AMBIGUOUS_ITEM

        # NOTE: `repair_issue_contract._sha256()` (which produced every
        # digest recorded on this item) returns a `sha256:`-prefixed
        # string, unlike THIS module's own bare-hex `_sha256()` -- prefix
        # explicitly here so the comparison is genuinely apples-to-apples,
        # not a same-named-but-differently-formatted function.
        recomputed_section_digest = f"sha256:{_sha256(_structural_item_rendered_block(item))}"
        if recomputed_section_digest != insertion.get("candidate_section_digest"):
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH

        anchor_heading = insertion.get("anchor_heading")
        anchor_start_line = insertion.get("anchor_start_line")
        anchor_digest = insertion.get("anchor_digest")
        relation = insertion.get("relation")
        if relation not in ("before", "after", "insert_contract_key", "replace_section_content"):
            return STRUCTURAL_REPAIR_APPLY_FAILURE_CONFLICTING_TARGETS

        matches = heading_index.get((anchor_heading or "").strip().casefold(), [])
        if len(matches) != 1:
            # The anchor heading this item recorded is no longer present
            # exactly once in the live body (renamed, removed, or now
            # duplicated) -- the environment this item's insertion decision
            # was made against no longer holds.
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH
        sec = matches[0]
        recomputed_anchor_digest = "sha256:" + _sha256(f"## {sec['heading']}")
        if recomputed_anchor_digest != anchor_digest:
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH

        start, end = _section_line_bounds(sections, len(live_lines))[id(sec)]
        if relation == "before":
            expected_anchor_line = start
        elif relation == "replace_section_content":
            expected_anchor_line = start
        else:  # "after" / "insert_contract_key"
            expected_anchor_line = end
        if expected_anchor_line != anchor_start_line:
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH

    return None


def _revalidate_owner_anchor_sources_before_dispatch(
    items: list[dict], *, repo: str, issue_number: int
) -> "str | None":
    """Freshly bind every owner-anchor item to its live source comment.

    Structural artifacts are untrusted handoffs. Immediately before the one
    controlled mutation dispatch, re-fetch each distinct owner anchor once and
    require the same comment identity, target Issue, OWNER association, and
    body revision that the producer recorded. Any source read or binding
    failure maps to the existing digest-mismatch fail-closed outcome.
    """
    by_comment_id: dict[str, list[dict]] = {}
    for item in items:
        source_span = item.get("source_span")
        if not isinstance(source_span, dict) or source_span.get("authority_kind") != "owner_anchor":
            continue
        comment_id = source_span.get("source_object_id")
        if (
            source_span.get("source_repo") != repo
            or source_span.get("source_object_kind") != "issue_comment"
            or not isinstance(comment_id, str)
            or not comment_id.isdecimal()
            or item.get("source_url")
            != f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{comment_id}"
            or not isinstance(source_span.get("source_revision"), str)
        ):
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH
        by_comment_id.setdefault(comment_id, []).append(source_span)

    for comment_id, source_spans in by_comment_id.items():
        try:
            live_comment, source_error = _fetch_single_comment(repo, int(comment_id))
        except Exception:
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH
        if not isinstance(live_comment, dict) or source_error:
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH
        expected_url = f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{comment_id}"
        live_body = live_comment.get("body")
        if (
            not isinstance(live_body, str)
            or str(live_comment.get("id")) != comment_id
            or live_comment.get("html_url") != expected_url
            or live_comment.get("issue_url") != f"https://api.github.com/repos/{repo}/issues/{issue_number}"
            or live_comment.get("author_association") != "OWNER"
        ):
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH
        live_revision = f"sha256:{_sha256(live_body)}"
        if any(source_span.get("source_revision") != live_revision for source_span in source_spans):
            return STRUCTURAL_REPAIR_APPLY_FAILURE_DIGEST_MISMATCH
    return None


def _synthesize_structural_repaired_body(items: list[dict], live_body: str) -> "tuple[str | None, str | None]":
    """Issue #2396: deterministically synthesize a SINGLE repaired body from
    every item's `insertion` metadata, applied against the live body from
    the bottom of the document upward (by descending anchor line) so an
    earlier (higher-line) splice never invalidates a not-yet-processed
    lower-line anchor. Items resolving to the exact same insertion point
    are concatenated in ascending `template_field_order` (the template's
    own top-to-bottom field order) rather than left in an arbitrary/input
    order.

    Callers MUST have already run `_verify_structural_items_against_live_body()`
    successfully against the SAME `live_body` -- this function re-derives
    section boundaries again (a second, independent parse) rather than
    trusting any state from that earlier call, so it never silently
    applies an insertion whose anchor turned out to be ambiguous/duplicate.

    Returns (new_body, None) on success, or (None, failure_code) if two
    items resolve to conflicting/overlapping targets that cannot be
    uniquely applied.
    """
    from repair_issue_contract import _parse_h2_sections, _section_line_bounds

    live_lines = live_body.split("\n")
    sections = _parse_h2_sections(live_body)
    bounds = _section_line_bounds(sections, len(live_lines))
    heading_index: dict[str, list[dict]] = {}
    for sec in sections:
        heading_index.setdefault(sec["heading"].strip().casefold(), []).append(sec)

    replace_plans: list[dict] = []
    insert_groups: dict[int, list[dict]] = {}

    for item in items:
        insertion = item["insertion"]
        relation = insertion["relation"]
        anchor_heading = insertion.get("anchor_heading")
        matches = heading_index.get((anchor_heading or "").strip().casefold(), [])
        if len(matches) != 1:
            return None, STRUCTURAL_REPAIR_APPLY_FAILURE_CONFLICTING_TARGETS
        sec = matches[0]
        start, end = bounds[id(sec)]

        if relation == "replace_section_content":
            replace_plans.append(
                {"start": start, "end": end, "item": item, "order": item["template_field_order"]}
            )
        elif relation == "before":
            insert_groups.setdefault(start - 1, []).append(item)
        else:  # "after" / "insert_contract_key"
            insert_groups.setdefault(end, []).append(item)

    # Reject overlapping replace ranges (fail closed rather than guess a
    # resolution order). In practice `replace_section_content` is only ever
    # reached by the producer for human_review_required items (Issue #2396
    # investigation), so this branch is unreachable on an
    # all-auto_apply_safe bundle -- kept as defense in depth.
    for i in range(len(replace_plans)):
        for j in range(i + 1, len(replace_plans)):
            a, b = replace_plans[i], replace_plans[j]
            if a["start"] <= b["end"] and b["start"] <= a["end"]:
                return None, STRUCTURAL_REPAIR_APPLY_FAILURE_CONFLICTING_TARGETS

    operations: list[tuple[int, int, list[str]]] = []
    for plan in replace_plans:
        operations.append((plan["start"] - 1, plan["end"], _structural_item_block_lines(plan["item"])))
    for pos, group in insert_groups.items():
        group.sort(key=lambda i: i["template_field_order"])
        combined_lines: list[str] = []
        for grouped_item in group:
            combined_lines.extend(_structural_item_block_lines(grouped_item))
            combined_lines.append("")
        operations.append((pos, pos, combined_lines))

    # Apply from the bottom of the body upward (descending start index) so
    # earlier splices never invalidate not-yet-processed lower indices.
    operations.sort(key=lambda op: op[0], reverse=True)
    for start_idx, end_idx, new_lines in operations:
        live_lines[start_idx:end_idx] = new_lines

    return "\n".join(live_lines), None


def _structural_items_already_applied(items: list[dict], live_body: str) -> bool:
    """Issue #2396 (AC10-analogue idempotent-replay short circuit): True
    when EVERY item's own target already reflects its candidate content in
    the live body -- i.e. this exact structural candidate was already
    applied by a prior invocation (or externally), so a replay must resolve
    to `no_change` without spending any dispatch."""
    from repair_issue_contract import _parse_h2_sections

    sections = _parse_h2_sections(live_body)
    heading_index: dict[str, list[dict]] = {}
    for sec in sections:
        heading_index.setdefault(sec["heading"].strip().casefold(), []).append(sec)

    for item in items:
        candidate_value = (item.get("candidate_value") or "").strip()
        field_id = item.get("field_id", "")
        if isinstance(field_id, str) and field_id.startswith("machine-readable-contract."):
            key_name = field_id.split(".", 1)[1]
            if (
                f"{key_name}: {candidate_value}" not in live_body
                and f'{key_name}: "{candidate_value}"' not in live_body
            ):
                return False
            continue
        label = item.get("label", "")
        matches = heading_index.get(label.strip().casefold(), [])
        if len(matches) != 1 or matches[0]["content"].strip() != candidate_value:
            return False
    return True


def _default_structural_fresh_validate_producer(body: str, covered_field_ids: "set[str]") -> dict:
    """Issue #2396 AC6 default fresh-validation producer: reruns the
    (narrow, mutation-free) structural section/contract-key detector
    against the fresh live body -- never the full `run_preflight()`
    pipeline, mirroring the generic lane's own `_default_fresh_validate_producer()`
    narrow-rerun scope -- and reports whether any of the ORIGINALLY covered
    `field_id`s still appear as a detected missing/invalid item."""
    if detect_missing_template_sections is None:
        return {"error": "structural_detector_unavailable", "actionable_repair": None}
    try:
        kind, template_text, template_path = _resolve_structural_repair_template(body, _find_repo_root())
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc), "actionable_repair": None}
    if kind is None:
        return {"error": "structural_template_unresolvable", "actionable_repair": None}
    try:
        fresh_items = detect_missing_template_sections(
            body, issue_kind=kind, template_text=template_text, template_path=template_path
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc), "actionable_repair": None}
    remaining = {i.get("field_id") for i in fresh_items if isinstance(i, dict)}
    return {"error": None, "actionable_repair": bool(remaining & covered_field_ids)}


def _run_structural_apply_fresh_validation(*, fetch, producer, expected_digest: "str | None") -> dict:
    """Issue #2396 AC6/AC9-analogue: post-mutation fresh validation. Re-reads
    the live Issue body (a read, never a mutation retry) and reruns
    `producer` against it. Succeeds ONLY when no actionable structural
    repair remains for the originally-covered fields AND the live body
    digest matches `expected_digest`."""
    if expected_digest is None:
        return {"status": "failed", "actionable_repair_remaining": None, "final_body_digest_match": None}
    try:
        fresh_issue = fetch()
    except Exception:
        return {"status": "failed", "actionable_repair_remaining": None, "final_body_digest_match": None}

    fresh_body = fresh_issue.get("body", "") or ""
    fresh_digest = _sha256(fresh_body)
    digest_match = _repair_apply_strip_sha_prefix(fresh_digest) == _repair_apply_strip_sha_prefix(expected_digest)

    produced = producer(fresh_body)
    if produced.get("error") is not None:
        return {"status": "failed", "actionable_repair_remaining": None, "final_body_digest_match": digest_match}

    actionable_remaining = produced.get("actionable_repair")
    status = "success" if (actionable_remaining is False and digest_match) else "failed"
    return {
        "status": status,
        "actionable_repair_remaining": actionable_remaining,
        "final_body_digest_match": digest_match,
    }


def run_structural_repair_action_apply(
    *,
    repo: str,
    issue_number: int,
    preflight_result_path: str,
    repo_root: "Path | None" = None,
    fetch_current=None,
    apply_transaction=None,
    fresh_validate=None,
) -> dict:
    """Issue #2396: `structural_repair_action.apply` controlled consumer.

    Reads a previously-produced preflight-result artifact via the SAME
    FD-based secure reader `run_repair_action_apply()` uses (AC7 of Issue
    #2039's own reader), requires `structural_repair_action.disposition_summary
    == auto_apply_safe` with every item ITSELF `auto_apply_safe` and no
    `insertion.disposition == "ambiguous"` item present (AC3), re-verifies
    every item's own recorded digests against the freshly-fetched LIVE
    Issue body (AC5), synthesizes a single whole-body candidate from the
    per-item insertion metadata (never a pre-materialized
    `candidate_body_artifact` -- no such artifact exists for this schema),
    and dispatches it through the EXACT SAME shared
    `_dispatch_candidate_body_default_transaction()` /
    `_dispatch_candidate_body_via_edit_txn()` core `run_repair_action_apply()`
    itself uses (AC5 -- never a second, independent GitHub-mutation
    implementation).

    A replay against a live body that already reflects every item's own
    candidate content resolves deterministically to `mutation_outcome:
    no_change` with no GitHub mutation dispatched, even when the whole body
    has otherwise drifted from `original_body_sha256` (AC10-analogue).
    """
    root = repo_root or _find_repo_root()
    _pf_path = Path(preflight_result_path)
    if not _pf_path.is_absolute():
        _pf_path = root / _pf_path
    try:
        result_text, _ = secure_read_repair_apply_artifact(_pf_path, root=root)
    except RepairApplySecureOpenError:
        return _structural_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )
    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        return _structural_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )
    if not isinstance(parsed, dict):
        return _structural_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    structural_repair_action = parsed.get("structural_repair_action")
    if not isinstance(structural_repair_action, dict) or (
        structural_repair_action.get("disposition_summary") != STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    ):
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=STRUCTURAL_REPAIR_APPLY_FAILURE_INVALID_DISPOSITION,
        )
    if structural_repair_action.get("repo") != repo or structural_repair_action.get("issue_number") != issue_number:
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=STRUCTURAL_REPAIR_APPLY_FAILURE_CROSS_ISSUE,
        )

    items = structural_repair_action.get("items")
    if (
        not isinstance(items, list)
        or not items
        or any(not isinstance(i, dict) or i.get("disposition") != STRUCT_DISPOSITION_AUTO_APPLY_SAFE for i in items)
    ):
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=STRUCTURAL_REPAIR_APPLY_FAILURE_INVALID_DISPOSITION,
        )

    original_body_sha256 = structural_repair_action.get("original_body_sha256")

    def _default_fetch_current():
        current_issue, issue_error = _fetch_issue(repo, issue_number)
        if current_issue is None:
            raise RuntimeError(f"issue_readback_failed:{issue_error}")
        return current_issue

    fetch = fetch_current or _default_fetch_current
    try:
        current_issue = fetch()
    except Exception:
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="precondition_read",
            failure_code=STRUCTURAL_REPAIR_APPLY_FAILURE_READBACK_UNRESOLVABLE,
        )

    live_body = current_issue.get("body", "") or ""
    live_body_digest = f"sha256:{hashlib.sha256(live_body.encode('utf-8')).hexdigest()}"

    if _repair_apply_strip_sha_prefix(live_body_digest) != _repair_apply_strip_sha_prefix(original_body_sha256):
        # Issue #2396 (stale guard, mirrors the generic lane's own
        # original_body_sha256 comparison): the whole body has drifted
        # since this bundle was produced. Before failing closed, check the
        # AC10-analogue idempotent-replay short circuit -- if every item's
        # own target already reflects its candidate content, this is a
        # replay of an already-applied change, not a fresh drift.
        if _structural_items_already_applied(items, live_body):
            return {
                "schema_version": STRUCTURAL_REPAIR_APPLY_STDOUT_SCHEMA_VERSION,
                "phase": "complete",
                "mutation_outcome": "no_change",
                "failure_code": None,
                "repo": repo,
                "issue_number": issue_number,
                "items_applied": 0,
                "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
                "receipt": {
                    "patch_attempted": False,
                    "executor_status": None,
                    "mutation_outcome": "no_change",
                    "failure_code": None,
                    "final_readback": {
                        "status": "verified",
                        "digest": live_body_digest,
                        "digest_class": "candidate",
                    },
                },
                "fresh_validation": {
                    "status": "not_run",
                    "actionable_repair_remaining": None,
                    "final_body_digest_match": None,
                },
            }
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="precondition_read",
            failure_code=STRUCTURAL_REPAIR_APPLY_FAILURE_BODY_DRIFT,
        )

    # AC5: per-item digest re-verification against the LIVE body, always --
    # even though the whole-body stale guard above already passed.
    _verify_failure = _verify_structural_items_against_live_body(items, live_body)
    if _verify_failure is not None:
        return _structural_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="provenance_validation", failure_code=_verify_failure
        )

    new_body, synth_failure = _synthesize_structural_repaired_body(items, live_body)
    if synth_failure is not None or new_body is None:
        return _structural_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="provenance_validation", failure_code=synth_failure
        )

    candidate_digest = f"sha256:{hashlib.sha256(new_body.encode('utf-8')).hexdigest()}"

    # Structural routing evaluates the real, synthesized whole body before
    # transaction dispatch. The result is digest-bound and reused by the
    # shared core on `go`; every other checker disposition stops here.
    # The checker receives a closed, ordinary path.  Keeping it in a private
    # temporary directory avoids the open NamedTemporaryFile pathname handoff
    # while retaining one ephemeral, per-invocation candidate file.
    with tempfile.TemporaryDirectory() as readiness_dir:
        readiness_path = Path(readiness_dir) / "candidate_body.md"
        readiness_path.write_text(new_body, encoding="utf-8")
        readiness = _structural_digest_bound_readiness(
            candidate_body=new_body,
            candidate_path=readiness_path,
        )
    readiness_status = readiness.get("status")
    if readiness_status != "go":
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="candidate_readiness",
            failure_code=_STRUCTURAL_READINESS_FAILURE_CODES.get(
                readiness_status,
                STRUCTURAL_REPAIR_APPLY_FAILURE_READINESS_INPUT_OR_RUNTIME_ERROR,
            ),
            readiness_diagnostics=_structural_readiness_diagnostics(readiness),
        )

    def _default_apply_transaction(current_issue_: dict, candidate_body: str) -> dict:
        # AC5: the EXACT SAME shared dispatch core the generic
        # repair_action.apply lane's own default uses -- never a second,
        # independent GitHub-mutation implementation. `scratch_subdir`
        # keeps this lane's scratch files in their own subdirectory.
        return _dispatch_candidate_body_default_transaction(
            current_issue_,
            candidate_body,
            root=root,
            issue_number=issue_number,
            repo=repo,
            scratch_subdir="structural-repair-action-apply",
            precomputed_readiness=readiness,
        )

    apply_fn = apply_transaction or _default_apply_transaction
    owner_anchor_failure = _revalidate_owner_anchor_sources_before_dispatch(
        items, repo=repo, issue_number=issue_number
    )
    if owner_anchor_failure is not None:
        return _structural_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=owner_anchor_failure,
        )
    txn_result = apply_fn(current_issue, new_body)

    def _resolve_readback() -> "str | None":
        fresh_issue = fetch()
        return f"sha256:{hashlib.sha256((fresh_issue.get('body', '') or '').encode('utf-8')).hexdigest()}"

    receipt = _repair_receipt_from_txn_result(
        txn_result,
        candidate_digest=candidate_digest,
        old_digest=live_body_digest,
        resolve_readback=_resolve_readback,
    )
    mutation_outcome = receipt["mutation_outcome"]
    phase = "complete" if mutation_outcome in {"applied", "no_change", "not_attempted"} else "final_readback"
    failure_code = receipt["failure_code"]

    if receipt.get("patch_attempted"):
        expected_digest = _repair_apply_expected_post_mutation_digest(
            mutation_outcome=mutation_outcome,
            receipt=receipt,
            candidate_digest=candidate_digest,
            old_digest=live_body_digest,
        )
        covered_field_ids = {i.get("field_id") for i in items}
        fresh_validate_fn = fresh_validate or (
            lambda body: _default_structural_fresh_validate_producer(body, covered_field_ids)
        )
        fresh_validation = _run_structural_apply_fresh_validation(
            fetch=fetch, producer=fresh_validate_fn, expected_digest=expected_digest
        )
    else:
        fresh_validation = {"status": "not_run", "actionable_repair_remaining": None, "final_body_digest_match": None}

    if fresh_validation["status"] == "failed" and phase == "complete":
        phase = "fresh_validation"
        failure_code = "fresh_validation_failed"

    return {
        "schema_version": STRUCTURAL_REPAIR_APPLY_STDOUT_SCHEMA_VERSION,
        "phase": phase,
        "mutation_outcome": mutation_outcome,
        "failure_code": failure_code,
        "repo": repo,
        "issue_number": issue_number,
        "items_applied": len(items) if mutation_outcome == "applied" else 0,
        "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
        "receipt": receipt,
        "fresh_validation": fresh_validation,
    }


# ---------------------------------------------------------------------------
# #2053: SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 producer / consumer.
#
# producer (this file, generate_authority_transport_manifest) -> router
# (decide_next_loop_action.py generate_router_receipt, via command_registry
# "decide.run") -> consumer (this file, consume_authority_transport).
#
# All three stages bind the same canonical payload digest
# (CANONICALIZATION_ID = "loop-protocol-json-c14n-v1", i.e. _canonical_json
# above: sorted keys, compact separators, UTF-8, no NaN/Infinity) and write
# their artifacts to an *immutable per-invocation directory* so a stale
# previous-invocation artifact can never be silently reused (AC10).
# ---------------------------------------------------------------------------

AUTHORITY_TRANSPORT_SCHEMA_VERSION = "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1"
CONSUMPTION_RECEIPT_SCHEMA_VERSION = "SCOPE_DELTA_CONSUMPTION_RECEIPT_V1"
CANONICALIZATION_ID = "loop-protocol-json-c14n-v1"

# #2053 P0 fix-delta (iteration 3, OWNER PR review): the controlled
# consumer's "mutation" step can operate in one of two lanes, always
# recorded on the consumption receipt so a reader never has to infer which
# happened from side effects alone:
#   - MUTATION_LANE_ARTIFACT_ONLY: no CONTRACT_PATCH_PLAN_V1 was supplied --
#     the consumer's bounded write is the local `consumed_authority_payload_v1
#     .json` audit artifact only (the pre-#2053-iteration-3 behavior).
#   - MUTATION_LANE_CONTRACT_PATCH_PLAN_CONSUMER: a CONTRACT_PATCH_PLAN_V1 and
#     anchor_context were supplied -- the mutation is delegated to the real,
#     existing controlled-mutation lane (consume_trusted_anchor_contract_
#     patch_plan() -> edit_issue_txn.py), and `mutation_applied` reflects that
#     lane's actual outcome, not merely the local artifact write succeeding.
MUTATION_LANE_ARTIFACT_ONLY = "artifact_only"
MUTATION_LANE_CONTRACT_PATCH_PLAN_CONSUMER = "contract_patch_plan_consumer"


def _authority_transport_dir(repo_root: Path, issue_number: int, invocation_id: str) -> Path:
    return (
        repo_root
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(issue_number)
        / "authority-transport"
        / invocation_id
    )


def _confine_artifact_path(path: "Path | None", repo_root: Path) -> "tuple[Path | None, str | None]":
    """#2053 P1 fix-delta: resolve `path` and confine it under
    <repo_root>/.claude/artifacts/, rejecting symlinks and non-regular
    files, before it is ever opened for reading. Router receipts and
    transport manifests are attacker-influenceable strings (they arrive as
    CLI arguments / receipt fields) -- without this check a
    symlink or a path outside the artifact root could be substituted.

    Returns (resolved_path, None) on success, or (None, reason_code) on any
    violation (fail-closed, never silently proceeds with an unconfined
    path).
    """
    if path is None:
        return None, "missing_file"
    artifact_root = (repo_root / ".claude" / "artifacts").resolve()
    if path.is_symlink():
        return None, "path_confinement_symlink_rejected"
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None, "path_confinement_resolve_failed"
    try:
        resolved.relative_to(artifact_root)
    except ValueError:
        return None, "path_confinement_outside_artifact_root"
    if resolved.exists() and not resolved.is_file():
        return None, "path_confinement_not_regular_file"
    return resolved, None


def _atomic_write_json_with_readback(path: Path, data: dict) -> tuple[bool, "dict | None", "str | None"]:
    """flush -> fsync -> exclusive-create, then read back and independently
    recompute the canonical digest to verify the bytes on disk match what
    was intended (#2053 AC10). Returns (ok, readback_data, error_reason).

    Fresh review blocker P1-C: every caller of this helper writes a
    single-invocation, exactly-once, "immutable" artifact guarded by its own
    prior `path.exists()` check (the producer's manifest, the consumer's
    consumed-payload record, and the consumer's consumption receipt). A
    plain `exists()`-check-then-`os.replace()` is a TOCTOU race: two
    concurrent writers for the SAME path can both pass the `exists()` check
    before either writes, and `os.replace()` unconditionally overwrites --
    whichever writer's `os.replace()` runs last silently wins, defeating the
    "immutable, exactly once" guarantee these callers rely on. This uses
    `os.link()` for the final publish step instead: `os.link()` atomically
    fails with `FileExistsError` if the destination already exists (a
    kernel-level exclusive-create guarantee, not a check-then-act race), so
    at most one concurrent writer for the same path can ever "win" -- the
    loser fails closed with `write_failure:already_exists` and writes
    nothing to `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{id(data)}"
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            return False, None, "write_failure:already_exists"
    except OSError as exc:
        return False, None, f"write_failure:{type(exc).__name__}:{exc}"
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

    try:
        readback_text = path.read_text(encoding="utf-8")
        readback_data = json.loads(readback_text)
    except (OSError, json.JSONDecodeError) as exc:
        return False, None, f"write_failure:{type(exc).__name__}:{exc}"

    if _sha256(_canonical_json(readback_data)) != _sha256(_canonical_json(data)):
        return False, readback_data, "write_failure:readback_digest_mismatch"

    return True, readback_data, None


def generate_authority_transport_manifest(
    *,
    evidence: Any,
    issue_number: int,
    repo: str,
    invocation_id: str,
    git_head_sha: str,
    repo_root: Path,
    generated_at: "str | None" = None,
) -> tuple["dict | None", "str | None"]:
    """#2053 AC1/AC7/AC10: build + immutably persist a
    SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest for `evidence` (a
    SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 dict, or a list of them).

    Returns (result, error). `result` is
    {"manifest": <dict>, "manifest_path": <str>} on success, None on
    failure (`error` explains why -- e.g. "no_evidence" or a write_failure
    reason from _atomic_write_json_with_readback).

    Writing under an invocation-scoped directory
    (authority-transport/<invocation_id>/) is what makes AC10's
    "stale previous-invocation artifact is never reused" true by
    construction: a caller must mint a fresh invocation_id to get a fresh
    manifest path; there is no shared mutable sidecar to go stale.
    """
    if evidence is None or (isinstance(evidence, list) and not evidence):
        return None, "no_evidence"

    payload = evidence
    payload_sha256 = _sha256(_canonical_json(payload))
    source = (
        evidence[0]
        if isinstance(evidence, list) and evidence
        else (evidence if isinstance(evidence, dict) else {})
    )
    manifest = {
        "schema_version": AUTHORITY_TRANSPORT_SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "repo": repo,
        "git_head_sha": git_head_sha,
        "generated_at": generated_at or _now_iso(),
        "canonicalization_id": CANONICALIZATION_ID,
        "source_comment_id": source.get("comment_id"),
        "source_comment_url": source.get("comment_url"),
        "source_issue_body_sha256": source.get("body_sha256"),
        "source_kind": source.get("source_kind") or "generated_by_agent",
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    # #2053 P1 fix-delta (iteration 3, OWNER PR review): actually enforce
    # SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 via _validate_with_schema() before
    # ever persisting it, not merely constructing the dict by hand above.
    # Fresh review blocker P1-A: schema enforcement is a real safety claim
    # for this mutation lane -- a missing/malformed schema file must fail
    # closed (schema_unavailable), never silently skip validation and
    # proceed to persist an unvalidated manifest.
    schema = _load_schema("scope_delta_authority_transport_v1.schema.json")
    if schema is None:
        return None, "schema_unavailable:scope_delta_authority_transport_v1.schema.json"
    valid, errors = _validate_with_schema(manifest, schema)
    if not valid:
        return None, f"schema_invalid:{errors[:1]}"

    manifest_dir = _authority_transport_dir(repo_root, issue_number, invocation_id)
    manifest_path = manifest_dir / "scope_delta_authority_transport_v1.json"
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): true immutability,
    # not just os.replace() pathing -- re-running the producer with the SAME
    # invocation_id must refuse to overwrite the artifact it already wrote,
    # rather than silently replacing it. A caller must mint a fresh
    # invocation_id to get a fresh manifest.
    if manifest_path.exists():
        return None, "manifest_already_exists"
    ok, _readback, error = _atomic_write_json_with_readback(manifest_path, manifest)
    if not ok:
        return None, error
    return {"manifest": manifest, "manifest_path": str(manifest_path)}, None


# #2086 P0 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 1/Blocker 2):
# `investigation_derived_path_literals` (scope_signal_delta.py) was
# previously a schema-external, caller-supplied `list[str]` with no
# cryptographic binding to anything -- the gate only checked syntactic
# safety of each literal, never who/what/when generated the list. That is
# exactly the "human approved a high-level goal" -> "human approved
# whatever exact paths the agent derived" authority-laundering risk a prior
# #2086 OWNER review flagged. This makes the evidence a TYPED, BOUND
# artifact instead of a raw list: it MUST be minted as a
# SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest via the EXISTING #2053
# producer (`generate_authority_transport_manifest()` above / the
# `authority_transport.produce` command_id, already real-subprocess
# dispatchable through `skill_runtime_exec.py` since this Issue's
# iteration-2 fix_delta), with `payload` shaped as a single-element list
# containing one dict: `{"comment_id", "comment_url", "body_sha256",
# "source_kind": "generated_by_agent", "path_literals": [...]}`. AC9
# forbids a NEW provenance schema/ledger/sidecar family -- this reuses the
# existing schema/producer/immutable-per-invocation-directory machinery
# verbatim; only the *payload contents* (an investigation-derived path
# inventory instead of raw comment evidence) differ, which the manifest's
# existing `payload: type ["object", "array"]` / `source_kind:
# "generated_by_agent"` already accommodate without a schema edit.
def _validate_investigation_evidence_transport(
    path: "Path | str | None",
    *,
    repo_root: Path,
    issue_number: int,
    repo: str,
    anchor_url: "str | None",
    base_issue_body_sha256: str,
    git_head_sha: str,
) -> "tuple[list | None, str | None]":
    """Load + cryptographically bind a read-only-investigation exact-path
    inventory before it is ever allowed to clear the `expands_allowed_paths`
    boundary for the operator-selected human-context lane
    (`scope_signal_delta._has_investigation_derived_allowed_path_literals`).

    Binding checked (fail-closed on ANY mismatch -- consistent with the
    existing #1952 mixed-literal all-or-nothing lock, a failed binding NEVER
    partially clears the boundary, it simply makes
    `investigation_derived_path_literals` absent for this invocation):
      - `path` is confined under `.claude/artifacts/` (no traversal/symlink,
        reuses `_confine_artifact_path()`)
      - `schema_version == SCOPE_DELTA_AUTHORITY_TRANSPORT_V1`, and the
        manifest validates against the existing (unmodified)
        `scope_delta_authority_transport_v1.schema.json`
      - `issue_number` / `repo` match this invocation's own issue/repo
      - `source_comment_url` matches the SAME human-context anchor comment
        URL this preflight invocation is processing (prevents binding an
        inventory derived for one directive to a different one)
      - `source_issue_body_sha256` matches the CURRENT live issue body
        (prevents stale-body inventory reuse after the body changed)
      - `git_head_sha` matches the current live repo HEAD (prevents
        cross-checkout / stale-commit replay)
      - `payload_sha256 == sha256(canonical_json(payload))` (content digest
        binding -- the payload cannot be swapped after the manifest was
        minted)
      - `payload` is `[{"comment_url": ..., "body_sha256": ...,
        "source_kind": "generated_by_agent", "path_literals": [str, ...]}]`
        and `path_literals` is a non-empty list of strings

    The *authorization* decision itself (is this literal set genuinely
    same-goal / bounded / a safe composition for THIS directive) is made
    once, here, by this root control-plane loader -- after binding
    validates -- never implicitly granted by the mere existence of a
    `list[str]` argument (the pre-fix_delta design).

    Returns (validated_path_literals, None) on success, or
    (None, reason_code) on any failure.
    """
    if not path:
        return None, "missing_transport_path"
    resolved, confinement_reason = _confine_artifact_path(Path(path), repo_root)
    if confinement_reason is not None:
        return None, confinement_reason
    if resolved is None or not resolved.exists():
        return None, "transport_file_missing"
    try:
        manifest = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"transport_malformed_json:{type(exc).__name__}"
    if not isinstance(manifest, dict):
        return None, "transport_not_object"
    if manifest.get("schema_version") != AUTHORITY_TRANSPORT_SCHEMA_VERSION:
        return None, "transport_schema_version_mismatch"
    schema = _load_schema("scope_delta_authority_transport_v1.schema.json")
    if schema is None:
        return None, "schema_unavailable:scope_delta_authority_transport_v1.schema.json"
    valid, errors = _validate_with_schema(manifest, schema)
    if not valid:
        return None, f"transport_schema_invalid:{errors[:1]}"
    if manifest.get("issue_number") != issue_number:
        return None, "transport_issue_number_mismatch"
    manifest_repo = manifest.get("repo")
    if not isinstance(manifest_repo, str) or manifest_repo.lower() != repo.lower():
        return None, "transport_repo_mismatch"
    if not anchor_url or manifest.get("source_comment_url") != anchor_url:
        return None, "transport_anchor_url_mismatch"
    if manifest.get("source_issue_body_sha256") != base_issue_body_sha256:
        return None, "transport_issue_body_sha256_mismatch"
    if manifest.get("git_head_sha") != git_head_sha:
        return None, "transport_git_head_sha_mismatch"
    if manifest.get("source_kind") != "generated_by_agent":
        return None, "transport_source_kind_mismatch"
    payload = manifest.get("payload")
    if _sha256(_canonical_json(payload)) != manifest.get("payload_sha256"):
        return None, "transport_payload_digest_mismatch"
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return None, "transport_payload_shape_invalid"
    entry = payload[0]
    if entry.get("comment_url") != anchor_url:
        return None, "transport_payload_comment_url_mismatch"
    if entry.get("body_sha256") != base_issue_body_sha256:
        return None, "transport_payload_body_sha256_mismatch"
    if entry.get("source_kind") != "generated_by_agent":
        return None, "transport_payload_source_kind_mismatch"
    literals = entry.get("path_literals")
    if not isinstance(literals, list) or not literals:
        return None, "transport_payload_literals_missing"
    for item in literals:
        if not isinstance(item, str):
            return None, "transport_payload_entry_not_string"
    return literals, None


def consume_authority_transport(
    *,
    router_receipt_path: str,
    issue_number: int,
    repo: str,
    invocation_id: str,
    git_head_sha: str,
    repo_root: Path,
    contract_patch_plan: "dict | None" = None,
    anchor_context: "dict | None" = None,
) -> dict:
    """#2053 AC9: controlled consumer. Reads a SCOPE_DELTA_ROUTER_RECEIPT_V1,
    independently re-verifies the transport manifest it references, applies
    at most one mutation (a deterministic, idempotency-guarded write under
    the same invocation-scoped directory), performs a readback, and a
    lightweight "fresh rerun" (re-running classify_scope_delta_authority()
    against the consumed payload to reconfirm the route is unchanged).

    Fail-closed for: missing_file, malformed_json, digest_mismatch,
    wrong_issue, wrong_git_head, wrong_invocation_id, router_receipt_not_ok,
    stale_previous_invocation (a consumption receipt already exists for this
    invocation_id -- the "exactly once" guard for AC9's "一回だけ mutation").

    #2053 P0 fix-delta (iteration 3, OWNER PR review): when `contract_patch_plan`
    (a CONTRACT_PATCH_PLAN_V1 dict) and `anchor_context` (a dict with
    `issue`, `anchor_url`, `anchor_payload`, `anchor_body`, and optionally
    `callbacks` / `known_context`) are both supplied, the mutation step is
    delegated to the existing, real controlled-mutation lane --
    `consume_trusted_anchor_contract_patch_plan()` (which, via its default
    callbacks, drives `edit_issue_txn.py`) -- instead of merely writing the
    local `consumed_authority_payload_v1.json` audit artifact. The receipt's
    `mutation_applied` claim reflects that lane's real, independently
    projected outcome (`_bounded_contract_update_handoff()`), never just the
    local artifact write succeeding. When `contract_patch_plan` /
    `anchor_context` are omitted (the default), behavior is unchanged from
    before this fix-delta: the bounded local artifact write is the mutation.
    Either way, `mutation_lane` on the receipt records which happened.
    """

    # #2053 AC10 P0 fix-delta (iteration 2): termination telemetry recording
    # generated/received/consumed artifacts by *relative path + sha256*,
    # not depending on any fixed mutable sidecar. Populated progressively as
    # each artifact is independently confirmed to exist on disk; every
    # _receipt() call (success or fail-closed) carries whatever subset was
    # confirmed by that point.
    _artifacts_seen: dict = {
        "generated_artifact": None,
        "received_artifact": None,
        "consumed_artifact": None,
    }

    def _artifact_ref(path: "Path | None") -> "dict | None":
        if path is None or not path.exists():
            return None
        try:
            relative_path = str(path.relative_to(repo_root))
        except ValueError:
            relative_path = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return {"relative_path": relative_path, "sha256": _sha256(text)}

    def _receipt(
        *,
        status: str,
        reason_code: "str | None",
        transport_payload_sha256: "str | None" = None,
        consumed_payload_sha256: "str | None" = None,
        mutation_applied: bool = False,
        readback_verified: bool = False,
        fresh_rerun_performed: bool = False,
        mutation_lane: str = MUTATION_LANE_ARTIFACT_ONLY,
        contract_update_status: "str | None" = None,
    ) -> dict:
        receipt = {
            "schema_version": CONSUMPTION_RECEIPT_SCHEMA_VERSION,
            "invocation_id": invocation_id,
            "issue_number": issue_number,
            "git_head_sha": git_head_sha,
            "generated_at": _now_iso(),
            "router_receipt_path": router_receipt_path,
            "transport_payload_sha256": transport_payload_sha256,
            "consumed_payload_sha256": consumed_payload_sha256,
            "mutation_applied": mutation_applied,
            "readback_verified": readback_verified,
            "fresh_rerun_performed": fresh_rerun_performed,
            "generated_artifact": _artifacts_seen["generated_artifact"],
            "received_artifact": _artifacts_seen["received_artifact"],
            "consumed_artifact": _artifacts_seen["consumed_artifact"],
            "status": status,
            "reason_code": reason_code,
            "mutation_lane": mutation_lane,
            "contract_update_status": contract_update_status,
        }
        if status == "ok":
            # #2053 P1 fix-delta (iteration 3, OWNER PR review): actually
            # enforce SCOPE_DELTA_CONSUMPTION_RECEIPT_V1 via
            # _validate_with_schema() (required/additionalProperties:false),
            # not merely a manual field-by-field construction above. An "ok"
            # receipt that fails schema validation is downgraded to a
            # fail-closed environment_failure -- schema conformance is a real
            # gate, not advisory.
            schema = _load_schema("scope_delta_consumption_receipt_v1.schema.json")
            if schema is None:
                receipt = dict(receipt)
                receipt["status"] = "environment_failure"
                receipt["reason_code"] = "schema_unavailable"
            else:
                valid, errors = _validate_with_schema(receipt, schema)
                if not valid:
                    receipt = dict(receipt)
                    receipt["status"] = "environment_failure"
                    receipt["reason_code"] = "schema_invalid"
        return receipt

    invocation_dir = _authority_transport_dir(repo_root, issue_number, invocation_id)
    consumption_receipt_path = invocation_dir / "scope_delta_consumption_receipt_v1.json"

    # AC10 / AC9 "exactly once": a consumption receipt already present for
    # this invocation_id means this invocation was already consumed -- never
    # mutate a second time from the same (possibly stale) transport.
    if consumption_receipt_path.exists():
        return _receipt(status="environment_failure", reason_code="stale_previous_invocation")

    receipt_path = Path(router_receipt_path) if router_receipt_path else None
    receipt_path, confinement_error = _confine_artifact_path(receipt_path, repo_root)
    if confinement_error is not None:
        return _receipt(status="environment_failure", reason_code=confinement_error)
    if not receipt_path.exists():
        return _receipt(status="environment_failure", reason_code="missing_file")
    _artifacts_seen["received_artifact"] = _artifact_ref(receipt_path)

    try:
        router_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if (
        not isinstance(router_receipt, dict)
        or router_receipt.get("schema_version") != "SCOPE_DELTA_ROUTER_RECEIPT_V1"
    ):
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if router_receipt.get("status") != "ok":
        return _receipt(status="environment_failure", reason_code="router_receipt_not_ok")

    if router_receipt.get("issue_number") != issue_number:
        return _receipt(status="environment_failure", reason_code="wrong_issue")
    if router_receipt.get("git_head_sha") != git_head_sha:
        return _receipt(status="environment_failure", reason_code="wrong_git_head")
    if router_receipt.get("invocation_id") != invocation_id:
        return _receipt(status="environment_failure", reason_code="wrong_invocation_id")

    manifest_path_str = router_receipt.get("transport_manifest_path")
    manifest_path = Path(manifest_path_str) if manifest_path_str else None
    manifest_path, confinement_error = _confine_artifact_path(manifest_path, repo_root)
    if confinement_error is not None:
        return _receipt(status="environment_failure", reason_code=confinement_error)
    if not manifest_path.exists():
        return _receipt(status="environment_failure", reason_code="missing_file")
    _artifacts_seen["generated_artifact"] = _artifact_ref(manifest_path)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if not isinstance(manifest, dict) or manifest.get("schema_version") != AUTHORITY_TRANSPORT_SCHEMA_VERSION:
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if manifest.get("issue_number") != issue_number:
        return _receipt(status="environment_failure", reason_code="wrong_issue")
    if manifest.get("git_head_sha") != git_head_sha:
        return _receipt(status="environment_failure", reason_code="wrong_git_head")
    if manifest.get("invocation_id") != invocation_id:
        return _receipt(status="environment_failure", reason_code="wrong_invocation_id")
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): PR #1332 previously
    # added expected_repo binding specifically to prevent same-issue-number/
    # cross-repo spoofing; that boundary was missing here. The consumer
    # must independently verify the manifest's own `repo` field, not just
    # the issue_number, against the caller-supplied expected repo.
    if manifest.get("repo") != repo:
        return _receipt(status="environment_failure", reason_code="wrong_repo")

    recomputed = _sha256(_canonical_json(manifest.get("payload")))
    manifest_payload_sha256 = manifest.get("payload_sha256")
    if recomputed != manifest_payload_sha256 or manifest_payload_sha256 != router_receipt.get(
        "transport_payload_sha256"
    ):
        return _receipt(
            status="environment_failure",
            reason_code="digest_mismatch",
            transport_payload_sha256=manifest_payload_sha256,
            consumed_payload_sha256=recomputed,
        )

    # --- mutation (once): write the consumed payload under the same
    # invocation-scoped directory. This bounded, idempotency-guarded local
    # write is always performed as the audit/readback record -- it is the
    # ONLY mutation performed when no contract_patch_plan is supplied
    # (MUTATION_LANE_ARTIFACT_ONLY). When a contract_patch_plan IS supplied
    # (below), it remains the readback record for the transported payload,
    # but the load-bearing mutation is delegated to the real controlled
    # consumer lane instead.
    consumed_path = invocation_dir / "consumed_authority_payload_v1.json"
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): bind the
    # transaction/idempotency state BEFORE mutation, not just via
    # post-mutation receipt presence -- a leftover consumed-payload artifact
    # from a crashed prior run of this SAME invocation_id (receipt never
    # published) must never be silently overwritten/re-applied.
    if consumed_path.exists():
        return _receipt(status="environment_failure", reason_code="stale_previous_invocation")
    consumed_record = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1_CONSUMED",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "payload": manifest.get("payload"),
        "payload_sha256": manifest_payload_sha256,
    }
    ok, readback, error = _atomic_write_json_with_readback(consumed_path, consumed_record)
    if not ok:
        return _receipt(
            status="environment_failure",
            reason_code="write_failure",
            transport_payload_sha256=manifest_payload_sha256,
        )
    mutation_applied = True
    readback_verified = (
        isinstance(readback, dict) and readback.get("payload_sha256") == manifest_payload_sha256
    )
    _artifacts_seen["consumed_artifact"] = _artifact_ref(consumed_path)

    # --- #2053 P0 fix-delta (iteration 3): real controlled-mutation lane.
    # When the caller supplies a CONTRACT_PATCH_PLAN_V1 and the anchor
    # context it applies to, delegate the actual mutation authorization to
    # the existing, real consumer (consume_trusted_anchor_contract_patch_plan
    # -> edit_issue_txn.py) instead of treating the local artifact write
    # above as sufficient. `mutation_applied` is overwritten by that lane's
    # real, independently-projected outcome.
    mutation_lane = MUTATION_LANE_ARTIFACT_ONLY
    contract_update_status: "str | None" = None
    if isinstance(contract_patch_plan, dict) and isinstance(anchor_context, dict):
        required_context_keys = ("issue", "anchor_url", "anchor_payload", "anchor_body")
        if all(key in anchor_context for key in required_context_keys):
            mutation_lane = MUTATION_LANE_CONTRACT_PATCH_PLAN_CONSUMER
            try:
                raw_consumer_result = consume_trusted_anchor_contract_patch_plan(
                    repo=repo,
                    issue_number=issue_number,
                    issue=anchor_context["issue"],
                    anchor_url=anchor_context["anchor_url"],
                    anchor_payload=anchor_context["anchor_payload"],
                    anchor_body=anchor_context["anchor_body"],
                    contract_patch_plan=contract_patch_plan,
                    callbacks=anchor_context.get("callbacks"),
                    known_context=anchor_context.get("known_context"),
                )
            except Exception as exc:  # noqa: BLE001 - captured as a fail-closed reason
                return _receipt(
                    status="environment_failure",
                    reason_code="contract_patch_plan_consumer_error",
                    transport_payload_sha256=manifest_payload_sha256,
                    consumed_payload_sha256=recomputed,
                    mutation_applied=False,
                    readback_verified=readback_verified,
                    mutation_lane=mutation_lane,
                    contract_update_status=f"{type(exc).__name__}:{exc}",
                )
            handoff = _bounded_contract_update_handoff(raw_consumer_result)
            contract_update_status = handoff.get("status")
            mutation_applied = contract_update_status in {"applied", "no_change", "rebased"}
            if not mutation_applied:
                return _receipt(
                    status="environment_failure",
                    reason_code="contract_patch_plan_consumer_failed",
                    transport_payload_sha256=manifest_payload_sha256,
                    consumed_payload_sha256=recomputed,
                    mutation_applied=False,
                    readback_verified=readback_verified,
                    mutation_lane=mutation_lane,
                    contract_update_status=contract_update_status,
                )

    # --- fresh rerun: re-run classification against the consumed payload to
    # reconfirm the route is unchanged (no silent drift between transport
    # and consumption). #2053 P0 fix-delta (iteration 2): this is a real
    # gate, not best-effort telemetry -- an exception, or a route that has
    # drifted away from contract_update_required (e.g. human_escalation),
    # fails the consumption closed instead of silently reporting
    # fresh_rerun_performed=true with status: ok.
    fresh_rerun_performed = False
    fresh_rerun_route_action = None
    fresh_rerun_error = None
    try:
        from scope_signal_delta import (
            SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED,
            classify_scope_delta_authority,
        )

        fresh_result = classify_scope_delta_authority(
            manifest.get("payload"),
            target_issue_number=issue_number,
            expected_repo=repo,
            base_issue_body_sha256=manifest.get("source_issue_body_sha256"),
        )
        fresh_rerun_route_action = (
            fresh_result.get("route", {}).get("action")
            if isinstance(fresh_result, dict)
            else None
        )
        fresh_rerun_performed = (
            fresh_rerun_route_action == SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED
        )
    except Exception as exc:  # noqa: BLE001 - captured as a fail-closed reason, not swallowed
        fresh_rerun_performed = False
        fresh_rerun_error = f"{type(exc).__name__}:{exc}"

    if not fresh_rerun_performed:
        return _receipt(
            status="environment_failure",
            reason_code="fresh_rerun_route_drift" if fresh_rerun_error is None else "fresh_rerun_error",
            transport_payload_sha256=manifest_payload_sha256,
            consumed_payload_sha256=recomputed,
            mutation_applied=mutation_applied,
            readback_verified=readback_verified,
            fresh_rerun_performed=False,
            mutation_lane=mutation_lane,
            contract_update_status=contract_update_status,
        )

    final_receipt = _receipt(
        status="ok",
        reason_code=None,
        transport_payload_sha256=manifest_payload_sha256,
        consumed_payload_sha256=recomputed,
        mutation_applied=mutation_applied,
        readback_verified=readback_verified,
        fresh_rerun_performed=fresh_rerun_performed,
        mutation_lane=mutation_lane,
        contract_update_status=contract_update_status,
    )
    if final_receipt["status"] == "ok":
        ok, _readback2, error2 = _atomic_write_json_with_readback(consumption_receipt_path, final_receipt)
        if not ok:
            final_receipt = _receipt(
                status="environment_failure",
                reason_code="write_failure",
                transport_payload_sha256=manifest_payload_sha256,
                consumed_payload_sha256=recomputed,
                mutation_applied=mutation_applied,
                readback_verified=readback_verified,
                fresh_rerun_performed=fresh_rerun_performed,
                mutation_lane=mutation_lane,
                contract_update_status=contract_update_status,
            )
    return final_receipt


def _as_string_list(
    value: Any,
    field_name: str,
    blockers: list[str],
) -> tuple[list[str], bool]:
    """Extract a list[str] payload or record a blocker and fail closed."""
    if not isinstance(value, list):
        blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: {field_name} must be a list")
        return [], False

    for item in value:
        if not isinstance(item, str):
            blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: {field_name} contains non-string item")
            return [], False

    return value, True


def _build_safe_rewrite_constraints(
    required_sections: list[str],
    required_contract_keys: list[str],
) -> dict[str, Any]:
    """Build a schema-safe rewrite constraints payload for fail-closed payload violations."""
    return {
        "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "rewrite_constraints": {
            "must_add_sections": required_sections,
            "must_add_contract_keys": required_contract_keys,
            "freeform_rewrite_forbidden": True,
        },
        "override_policy": {
            "allowed_reason_codes": [
                "missing_required_section",
                "missing_required_contract_key",
            ],
            "never_override_reason_codes": [
                "unknown_issue_kind",
                "issue_kind_policy_load_error",
                "contract_schema_parse_error",
                "template_resolution_error",
                "checker_internal_error",
            ],
            "overridable_in_current_result": [],
            "non_overridable_in_current_result": [],
        },
        "max_rewrite_attempts": 2,
        "no_progress_route": "human_judgment_required",
    }


def _ensure_json_serializable(value: Any, field_name: str, blockers: list[str]) -> bool:
    """Validate JSON serializability for deterministic stdout/hashing artifacts."""
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return True
    except (TypeError, ValueError) as exc:
        blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE}: {field_name} serialization error: {exc}")
        return False


def _find_repo_root() -> Path:
    """Walk up from this script to find the .git root."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume .claude/skills/issue-refinement-loop/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _load_schema(schema_filename: str) -> dict | None:
    """Load a JSON schema file from the schemas directory. Returns None if not found."""
    schema_path = _SCHEMAS_DIR / schema_filename
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _validate_with_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    """
    Validate data against schema using jsonschema.

    Returns (is_valid, error_messages).

    Fresh review blocker P1-A: schema enforcement is a real safety claim for
    the authority-transport mutation lanes (generate_authority_transport_manifest
    / consume_authority_transport's "ok" receipt gate). If jsonschema is
    unavailable, this fails CLOSED (returns False with reason code
    "schema_validator_unavailable") -- it must never be silently converted
    to a passing validation.
    """
    if not _JSONSCHEMA_AVAILABLE:
        return False, ["schema_validator_unavailable: jsonschema library not importable"]
    try:
        validator_cls = _jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator = validator_cls(
            schema,
            format_checker=validator_cls.FORMAT_CHECKER,
        )
        errors = sorted(validator.iter_errors(data), key=lambda exc: list(exc.path))
        if errors:
            return False, [f"schema_validation_error: {errors[0].message}"]
        format_errors = _validate_date_time_formats(data, schema)
        if format_errors:
            return False, format_errors
        return True, []
    except _jsonschema.ValidationError as exc:
        return False, [f"schema_validation_error: {exc.message}"]
    except Exception as exc:
        return False, [f"schema_validation_unexpected: {exc}"]


def _validate_date_time_formats(data: Any, schema: dict, path: str = "$") -> list[str]:
    schema_type = schema.get("type")
    if schema.get("format") == "date-time" and isinstance(data, str):
        candidate = data.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            return [f"schema_validation_error: {path} must be a valid date-time"]
        return []

    if schema_type == "object" and isinstance(data, dict):
        errors: list[str] = []
        for key, value in data.items():
            child_schema = schema.get("properties", {}).get(key)
            if isinstance(child_schema, dict):
                errors.extend(_validate_date_time_formats(value, child_schema, f"{path}.{key}"))
        return errors

    if schema_type == "array" and isinstance(data, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            errors: list[str] = []
            for index, item in enumerate(data):
                errors.extend(_validate_date_time_formats(item, item_schema, f"{path}[{index}]"))
            return errors

    return []


# ---------------------------------------------------------------------------
# URL parsing for anchor comment structural validation
# ---------------------------------------------------------------------------

# Pattern: https://github.com/<owner>/<repo>/issues/<number>#issuecomment-<id>
_ISSUE_COMMENT_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/issues/(?P<issue_number>\d+)#issuecomment-(?P<comment_id>\d+)$"
)

# PR review comment pattern (different from issue comment)
_PR_REVIEW_COMMENT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#issuecomment-\d+$")
_PR_REVIEW_DISCUSSION_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#discussion_r\d+$")

# Valid --repo pattern
_REPO_PATTERN = re.compile(r"^[^/]+/[^/]+$")

# Valid GitHub comment URL prefix
_GITHUB_URL_PREFIX = "https://github.com/"


def _parse_anchor_comment_url(url: str) -> dict[str, Any]:
    """
    Parse an anchor comment URL into its structural components.

    Returns dict with: owner, repo, issue_number (int), comment_id (int), valid (bool)
    Does NOT use substring matching — validates URL structure via regex only.
    """
    # Reject PR review comment URLs (different endpoint from issue comments)
    if _PR_REVIEW_DISCUSSION_RE.match(url):
        return {"valid": False, "error": "pr_review_comment_url"}

    m = _ISSUE_COMMENT_RE.match(url)
    if not m:
        return {"valid": False, "error": "url_parse_failure"}

    return {
        "valid": True,
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "issue_number": int(m.group("issue_number")),
        "comment_id": int(m.group("comment_id")),
    }


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------


def _run_gh(argv: list[str], timeout: int = GH_API_TIMEOUT) -> tuple[dict | list | None, str]:
    """
    Run a gh command and return (parsed_json, error_message).

    Uses subprocess.run([...], shell=False) — never shell=True.
    Returns (None, error_message) on timeout, non-zero exit, or JSON parse failure.
    """
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None, "gh_not_found"
    except subprocess.TimeoutExpired:
        return None, f"gh_timeout after {timeout}s"
    except Exception as exc:
        return None, f"gh_unexpected_error: {exc}"

    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "")[:300]
        return None, f"gh_exit_{proc.returncode}: {stderr_snip}"

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh_json_decode_error: {exc}"

    return parsed, ""


def _is_isolated_claude_gpt_profile() -> bool:
    """Detect a Claude-GPT isolated HOME (Issue #2241 AC8).

    `scripts/claude-gpt/launch.sh` overrides `HOME` (and
    `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`) to a fresh sandbox directory before
    spawning the isolated Claude child process. Since Issue #2299 it ALSO
    shares GitHub auth with that child natively (a `GH_CONFIG_DIR` fixed
    before isolation; `GH_TOKEN`/`GITHUB_TOKEN`/`GH_HOST`/`GH_REPO` are not
    scrubbed), so this signal says ONLY "HOME is an isolated sandbox". It
    is NOT a proxy for "GitHub auth is absent" and MUST NOT be used to
    select the read transport (Issue #2872): transport selection is
    independent of HOME isolation and of GitHub auth availability.

    The real OS account home (resolved via the passwd database, never via
    `HOME`) differs from the current `HOME` env var only inside that
    launcher's sandbox -- never in a normal human/dev/CI shell. Retained as
    an independent axis for focused tests and diagnostics.
    """
    current_home = os.environ.get("HOME")
    if not current_home:
        return False
    try:
        real_home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return False
    return current_home != real_home


# ---------------------------------------------------------------------------
# Issue #2257 P0-1: single read-authority transport adapters.
#
# `run_preflight()` selects exactly ONE of these once per invocation (via
# `_select_read_transport()`) and threads it explicitly through every anchor
# comment call site on the read path -- `_fetch_single_comment`,
# `_validate_anchor_comment_url`, and `_validate_anchor_comments_batch` all
# accept a `transport` parameter. `_fetch_issue`/`_fetch_issue_comments` also
# accept it, but several pre-existing tests outside this module patch them
# with fixed-arity doubles, so `run_preflight()` leaves them on the
# `transport=None` default (and so do the repair-apply/trusted-anchor
# `_fetch_*` call sites). That is still single-authority because
# `_select_read_transport()` is a pure constant: it never consults HOME,
# GitHub auth, rate-limit state or a prior failure, so every resolution --
# explicit or via the `None` default -- yields the SAME native-`gh` transport
# instance for the whole process (Issue #2872; the split-brain pattern that
# caused Issue #2197's anchor comment 5315264311 misclassification cannot
# recur without a state-dependent selector). Never add one.
# ---------------------------------------------------------------------------


def _classify_credentialless_exception(exc: BaseException) -> str:
    """Map any exception raised by the credentialless transport to a fixed,
    closed-enum reason code (Issue #2257 AC3/AC4/P0-4).

    Never transcribes `str(exc)` -- an exception type this function does not
    recognize maps to the fixed code `transport_internal_error`, never to a
    string built from the exception's own message (which could echo
    URL/host/OS-error detail this module must never surface downstream)."""
    if _credentialless_read is None:
        return "credentialless_transport_unavailable"
    mapping = (
        (_credentialless_read.CanonicalResourceMissing, "canonical_resource_missing"),
        (_credentialless_read.UnexpectedAuthenticationDependency, "unexpected_authentication_dependency"),
        (_credentialless_read.RateLimitedRejected, "rate_limited"),
        (_credentialless_read.ForbiddenRejected, "http_403_forbidden"),
        (_credentialless_read.UpstreamEnvironmentFailure, "upstream_environment_failure"),
        (_credentialless_read.TransportConnectivityFailure, "transport_connectivity_failure"),
        (_credentialless_read.MalformedResponseBody, "malformed_response_body"),
        (_credentialless_read.CrossRepositoryReadRejected, "cross_repository_read_rejected"),
        (_credentialless_read.InvalidIssueNumberRejected, "invalid_issue_number"),
        (_credentialless_read.InvalidCommentIdRejected, "invalid_comment_id"),
    )
    for exc_cls, code in mapping:
        if isinstance(exc, exc_cls):
            return code
    if isinstance(exc, _credentialless_read.CredentiallessReadError):
        return "credentialless_read_error"
    return "transport_internal_error"


class _CredentiallessPreflightTransport:
    """`GitHubReadTransport`-shaped adapter (credentialless side) returning
    this module's `(data, err)` tuple convention. `err`, when set, is always
    a fixed, closed-enum reason code prefixed with `_TRANSPORT_FAILURE_PREFIX`
    or `_SEMANTIC_MISSING_PREFIX` -- never a transcribed exception string
    (Issue #2257 P0-1/P0-4)."""

    SOURCE_LABEL = "credentialless_transport"

    def __init__(self) -> None:
        self._inner = _credentialless_read.CredentiallessGitHubReadTransport()

    def read_issue(self, repo: str, issue_number: int) -> tuple[dict | None, str]:
        try:
            return self._inner.read_issue(repo, issue_number), ""
        except Exception as exc:  # noqa: BLE001 - classified below, never re-raised bare
            return None, f"{_TRANSPORT_FAILURE_PREFIX}{_classify_credentialless_exception(exc)}"

    def list_issue_comments(self, repo: str, issue_number: int) -> tuple[list | None, str]:
        try:
            comments = self._inner.list_issue_comments(repo, issue_number)
        except Exception as exc:  # noqa: BLE001 - classified below, never re-raised bare
            return None, f"{_TRANSPORT_FAILURE_PREFIX}{_classify_credentialless_exception(exc)}"
        if not isinstance(comments, list):
            return None, f"{_TRANSPORT_FAILURE_PREFIX}credentialless_comments_unexpected_type"
        return comments, ""

    def read_issue_comment(self, repo: str, comment_id: int) -> tuple[dict | None, str]:
        try:
            data = self._inner.read_issue_comment(repo, comment_id)
        except _credentialless_read.CanonicalResourceMissing:
            return None, f"{_SEMANTIC_MISSING_PREFIX}canonical_resource_missing"
        except Exception as exc:  # noqa: BLE001 - classified below, never re-raised bare
            return None, f"{_TRANSPORT_FAILURE_PREFIX}{_classify_credentialless_exception(exc)}"
        return data, ""


class _GhCliPreflightTransport:
    """`GitHubReadTransport`-shaped adapter (gh CLI side, non-isolated
    profile) preserving the exact pre-#2257 `gh` invocations and error
    strings for `read_issue`/`list_issue_comments` (AC8: no regression to
    the existing non-isolated path), while giving `read_issue_comment` the
    same sanitized semantic/transport classification `_fetch_single_comment`
    has always applied to the gh single-comment fetch."""

    SOURCE_LABEL = "gh_cli"

    def read_issue(self, repo: str, issue_number: int) -> tuple[dict | None, str]:
        return _run_gh(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "number,title,body,labels,url,updatedAt",
            ]
        )

    def list_issue_comments(self, repo: str, issue_number: int) -> tuple[list | None, str]:
        try:
            from command_registry import render_command as _render_command

            _argv = _render_command("gh.issue.comments.list", {"repo": repo, "issue_number": issue_number})
        except Exception as exc:
            raise RuntimeError(f"BLOCKER_COMMAND_REGISTRY_UNAVAILABLE: gh.issue.comments.list failed: {exc}") from exc
        data, err = _run_gh(_argv)
        if data is None:
            return None, err
        # --slurp wraps each page as an element: [[page1_comments...], [page2_comments...]]
        # Flatten one level regardless of page count.
        if isinstance(data, list):
            if len(data) == 0:
                return [], ""
            # Check if it's a slurp-wrapped list-of-lists
            if all(isinstance(item, list) for item in data):
                flattened: list[dict] = []
                for page in data:
                    flattened.extend(page)
                return flattened, ""
            # Already a flat list (e.g. non-paginated gh or mock returning flat list)
            return data, ""
        return None, f"gh_comments_unexpected_type: {type(data).__name__}"

    def read_issue_comment(self, repo: str, comment_id: int) -> tuple[dict | None, str]:
        data, err = _run_gh(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}"])
        if data is not None:
            return data, ""
        return None, _classify_gh_single_comment_error(err)


def _reset_read_transport_cache() -> None:
    """No-op retained for call-site compatibility. Since Issue #2872 the
    selector is a pure constant (`_select_read_transport()` never depends on
    HOME, GitHub auth or any prior read outcome), so there is no selection
    state to cache or clear. `run_preflight()` still calls this once at the
    top of its live-mode branch to document the single-authority selection
    point."""
    return None


# Issue #2872: a single process-wide instance. `_GhCliPreflightTransport` is
# stateless (every method shells out to `gh` afresh), so sharing one instance
# makes "the same transport for every read in one invocation" an identity
# property rather than a convention.
_NATIVE_GH_READ_TRANSPORT = _GhCliPreflightTransport()


def _select_read_transport() -> "_GhCliPreflightTransport":
    """Return the read-authority transport for every production read
    (Issue #2872): native `gh`, always.

    Authentication (env token, stored `GH_CONFIG_DIR` config, keyring) is
    resolved by `gh` itself. This selector deliberately does NOT inspect
    `HOME`, `GH_TOKEN`, `hosts.yml`, `GH_CONFIG_DIR` or `gh auth status`,
    and never falls back to the anonymous credentialless transport: an
    isolated HOME is not evidence that GitHub auth is absent since the
    #2299 launcher contract shares GitHub auth natively, and switching to an
    anonymous 60 req/hour budget (Issue #2845) or mid-invocation authority
    splitting (Issue #2257) is the failure mode this fixes. A genuinely
    authless environment surfaces as a structured `environment_failure`
    (`gh_auth_required`) from the `gh` read itself, not a secret probe.

    `run_preflight()` calls this once per invocation; every other caller
    that resolves it (the `transport=None` defaults of `_fetch_*`) gets the
    exact same instance (`_NATIVE_GH_READ_TRANSPORT`), so no read can land
    on a different authority."""
    return _NATIVE_GH_READ_TRANSPORT


def _fetch_issue(
    repo: str, issue_number: int, transport: "object | None" = None
) -> tuple[dict | None, str]:
    """Fetch issue data via `transport` (Issue #2257 P0-1: `transport` is
    the single read authority selected once by `_select_read_transport()`).

    `transport=None` (call sites that do not pass one explicitly) resolves
    `_select_read_transport()`, which is a constant (Issue #2872), so it is
    the same native-`gh` instance an explicit caller would have passed.
    """
    if transport is None:
        transport = _select_read_transport()
    if transport is None:
        return None, "BLOCKER_CREDENTIALLESS_TRANSPORT_UNAVAILABLE: github_credentialless_read import failed"
    return transport.read_issue(repo, issue_number)


def _fetch_issue_comments(
    repo: str, issue_number: int, transport: "object | None" = None
) -> tuple[list | None, str]:
    """Fetch all issue comments via `transport` (Issue #2257 P0-1 -- see
    `_fetch_issue` docstring for the single-authority contract and the
    `transport=None` back-compat note).

    gh 2.88.1+ --slurp returns [[...page1...], [...page2...]] which must be
    flattened to a single list.  Single-page results are also wrapped as [[...]].
    """
    if transport is None:
        transport = _select_read_transport()
    if transport is None:
        return None, "BLOCKER_CREDENTIALLESS_TRANSPORT_UNAVAILABLE: github_credentialless_read import failed"
    return transport.list_issue_comments(repo, issue_number)


# Issue #2257 AC3/AC4: `err` is prefixed with one of these two sanitized
# classification tags so callers can distinguish "the comment genuinely
# does not exist" (semantic_missing) from "the fetch itself failed"
# (transport_failure) without ever exposing credential/token/header/raw
# response/OS error detail downstream.
_SEMANTIC_MISSING_PREFIX = "semantic_missing:"
_TRANSPORT_FAILURE_PREFIX = "transport_failure:"


def _is_transport_failure(err: str) -> bool:
    """True iff `err` (as returned by `_fetch_single_comment`) is a
    transport failure rather than a semantic "comment not found"
    (Issue #2257 AC2/AC3)."""
    return err.startswith(_TRANSPORT_FAILURE_PREFIX)


# Issue #2257 AC10: closed enum of `reason_code` values the
# `refinement_preflight_result_v1` schema's `status == environment_failure`
# structured projection accepts. Any reason string that does not map onto
# this set collapses to the fixed fallback `transport_internal_error` --
# never an unbounded/echoed value.
_ENVIRONMENT_FAILURE_REASON_CODES = frozenset(
    {
        "unexpected_authentication_dependency",
        "rate_limited",
        "http_403_forbidden",
        "canonical_resource_missing",
        "upstream_environment_failure",
        "transport_connectivity_failure",
        "malformed_response_body",
        "cross_repository_read_rejected",
        "invalid_issue_number",
        "invalid_comment_id",
        "credentialless_read_error",
        "credentialless_transport_unavailable",
        "credentialless_comments_unexpected_type",
        "gh_auth_required",
        "gh_not_found",
        "gh_timeout",
        "gh_json_decode_error",
        "gh_unexpected_error",
        "gh_exit_error",
        "gh_unclassified_error",
        "transport_internal_error",
        "planner_internal_error",
        "planner_invocation_missing",
    }
)


def _classify_final_environment_failure(blockers: list[str], planner_exit_code: Optional[int]) -> str:
    """Classify the `reason_code` for the FINAL (post-planner) result build
    when `_apply_exit_code_mapping()` has computed `status ==
    "environment_failure"` (Issue #2257 AC10). This is a distinct call site
    from the early-return transport-failure paths above (which already carry
    their own precise reason via `_project_environment_failure_reason`):
    here the failure may stem from a pre-planner blocker
    (`BLOCKER_GH_FAILURE`/`BLOCKER_RESULT_SCHEMA_INVALID`/
    `BLOCKER_REPAIR_ENVIRONMENT_FAILURE`) or from the planner subprocess
    itself (`planner_exit_code is None` or `== 3`). Always returns a member
    of the closed `_ENVIRONMENT_FAILURE_REASON_CODES` enum."""
    for b in blockers:
        if b.startswith("ANCHOR_FETCH_REASON:"):
            candidate = b[len("ANCHOR_FETCH_REASON:"):]
            if candidate in _ENVIRONMENT_FAILURE_REASON_CODES:
                return candidate
    if any(b == BLOCKER_GH_FAILURE for b in blockers):
        return "transport_internal_error"
    if any(b == BLOCKER_RESULT_SCHEMA_INVALID for b in blockers):
        return "transport_internal_error"
    if planner_exit_code is None:
        return "planner_invocation_missing"
    return "planner_internal_error"


# Issue #2872 (PR #2876 review P2): confirmable EXTERNAL GitHub outage /
# connectivity evidence in `gh`'s own stderr snippet. Closed, bounded
# patterns only -- everything else (including every unrecognised exit 1) stays
# an unclassified `gh_exit_error`, so an implementation/propagation defect is
# never relabelled as an external outage. `gh` exits 1 for HTTP errors and
# connection failures alike, so the exit code alone cannot tell them apart.
_GH_OUTAGE_STDERR_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("rate_limited", re.compile(r"api rate limit exceeded|secondary rate limit|http 429", re.IGNORECASE)),
    ("upstream_environment_failure", re.compile(r"http 5\d\d\b", re.IGNORECASE)),
    (
        "transport_connectivity_failure",
        re.compile(
            r"error connecting to|dial tcp|no such host|connection refused|connection reset"
            r"|i/o timeout|tls handshake timeout|network is unreachable|temporary failure in name resolution",
            re.IGNORECASE,
        ),
    ),
)


def _classify_gh_outage_from_stderr(detail: str) -> Optional[str]:
    """Return the closed external-outage reason code evidenced by a `gh`
    stderr snippet, or None when the snippet does not confirm one (Issue
    #2872). Pure; never echoes any part of `detail`."""
    for reason_code, pattern in _GH_OUTAGE_STDERR_PATTERNS:
        if pattern.search(detail or ""):
            return reason_code
    return None


def _project_environment_failure_reason(err: str) -> str:
    """Map a raw `(data, err)` failure string from any fetch function in
    this module onto the closed `_ENVIRONMENT_FAILURE_REASON_CODES` enum
    (Issue #2257 AC3/AC4/AC10). Never returns `err` (or any substring built
    from an underlying exception's own message) verbatim when it is not
    already one of the closed, fixed reason codes this module's transport
    adapters emit -- an unrecognized shape falls back to the fixed code
    `transport_internal_error` rather than transcribing raw detail into the
    canonical result artifact."""
    reason = err or ""
    if reason.startswith(_TRANSPORT_FAILURE_PREFIX):
        reason = reason[len(_TRANSPORT_FAILURE_PREFIX):]
    elif reason.startswith(_SEMANTIC_MISSING_PREFIX):
        reason = reason[len(_SEMANTIC_MISSING_PREFIX):]
    elif reason.startswith("gh_exit_4:"):
        # gh CLI's documented "authentication required" exit code: a
        # genuinely authless environment surfaces here as a structured
        # environment_failure (Issue #2872), never as a secret probe.
        reason = "gh_auth_required"
    elif reason.startswith("gh_exit_"):
        # Only a stderr-confirmed external outage is projected onto the
        # outage reason codes; every other exit stays `gh_exit_error`.
        reason = _classify_gh_outage_from_stderr(reason.partition(":")[2]) or "gh_exit_error"
    elif reason.startswith("gh_not_found"):
        reason = "gh_not_found"
    elif reason.startswith("gh_timeout"):
        reason = "gh_timeout"
    elif reason.startswith("gh_json_decode_error"):
        reason = "gh_json_decode_error"
    elif reason.startswith("gh_unexpected_error"):
        reason = "gh_unexpected_error"
    elif reason.startswith("gh_comments_unexpected_type"):
        reason = "gh_unclassified_error"
    elif reason.startswith("BLOCKER_CREDENTIALLESS_TRANSPORT_UNAVAILABLE"):
        reason = "credentialless_transport_unavailable"
    if reason not in _ENVIRONMENT_FAILURE_REASON_CODES:
        reason = "transport_internal_error"
    return reason


def _classify_gh_single_comment_error(err: str) -> str:
    """Classify a `_run_gh` error string for the single-comment fetch path
    (Issue #2257 AC3): only a genuine HTTP 404 is `semantic_missing`;
    every other gh CLI failure (auth dependency incl. gh exit 4, timeout,
    gh not found, JSON decode failure, any other non-zero exit/HTTP
    status) is `transport_failure`. Returns a sanitized reason -- never
    the raw stderr snippet (AC4)."""
    if err.startswith("gh_exit_"):
        rest = err[len("gh_exit_"):]
        code_str, _, detail = rest.partition(":")
        detail_lower = detail.strip().lower()
        if "404" in detail_lower or "not found" in detail_lower:
            return f"{_SEMANTIC_MISSING_PREFIX}gh_404_not_found"
        code_str = code_str.strip()
        if code_str == "4":
            # gh CLI's documented "authentication required" exit code --
            # this is exactly the isolated-profile failure mode Issue
            # #2197 misclassified as ANCHOR_COMMENT_NOT_FOUND.
            return f"{_TRANSPORT_FAILURE_PREFIX}gh_auth_required"
        outage = _classify_gh_outage_from_stderr(detail)
        if outage is not None:
            return f"{_TRANSPORT_FAILURE_PREFIX}{outage}"
        return f"{_TRANSPORT_FAILURE_PREFIX}gh_exit_{code_str}"
    if err.startswith("gh_not_found"):
        return f"{_TRANSPORT_FAILURE_PREFIX}gh_not_found"
    if err.startswith("gh_timeout"):
        return f"{_TRANSPORT_FAILURE_PREFIX}gh_timeout"
    if err.startswith("gh_json_decode_error"):
        return f"{_TRANSPORT_FAILURE_PREFIX}gh_json_decode_error"
    if err.startswith("gh_unexpected_error"):
        return f"{_TRANSPORT_FAILURE_PREFIX}gh_unexpected_error"
    return f"{_TRANSPORT_FAILURE_PREFIX}gh_unclassified_error"


def _fetch_single_comment(
    repo: str, comment_id: int, transport: "object | None" = None
) -> tuple[dict | None, str]:
    """Fetch a single issue comment (Issue #2257 AC1/AC2/AC3/P0-1) via
    `transport` -- the single read authority selected once by
    `_select_read_transport()` and threaded down from `run_preflight()`.
    This function never re-evaluates HOME or any GitHub auth state to pick a
    transport (Issue #2872).

    `transport=None` (call sites that do not pass one explicitly) resolves
    `_select_read_transport()`, a constant native-`gh` selector (Issue
    #2872), so it is the same instance an explicit caller would pass.

    On failure, `err` is prefixed with either `_SEMANTIC_MISSING_PREFIX`
    (the comment genuinely does not exist at the canonical URL -- a true
    404) or `_TRANSPORT_FAILURE_PREFIX` (every other failure: auth
    dependency, rate limit, 5xx, DNS/connection, timeout, malformed JSON,
    incomplete pagination, read authority divergence). Callers must route
    these two categories differently -- only `semantic_missing` may be
    treated as "comment not found" (AC2); `transport_failure` must never
    be misreported as a missing comment (AC3). The sanitized reason after
    the prefix never contains credential/token/header/raw response/OS
    error detail (AC4).
    """
    if transport is None:
        transport = _select_read_transport()
    if transport is None:
        return None, f"{_TRANSPORT_FAILURE_PREFIX}credentialless_transport_unavailable"
    return transport.read_issue_comment(repo, comment_id)


def _load_anchor_comment_schema() -> dict[str, Any]:
    schema_path = _SCHEMAS_DIR / "anchor_comment.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ANCHOR_SCOPE_REFRAME_V1 parsing and classification (AC2-AC5)
# ---------------------------------------------------------------------------


def _parse_anchor_scope_reframe_body(comment_body: str) -> "dict | None":
    """
    Parse ANCHOR_SCOPE_REFRAME_V1 payload from a comment body.

    Only top-level fenced yaml blocks are canonical.
    Fail-closed: blockquote-embedded fenced blocks and raw-text markers are rejected.
    Returns None if not found or malformed.
    """
    import re

    fenced_pattern = re.compile(r"^```yaml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
    for match in fenced_pattern.finditer(comment_body):
        yaml_content = match.group(1)
        # Fail-closed: reject if this fence is inside a blockquote
        start = match.start()
        before = comment_body[:start]
        if before.rstrip().endswith(">"):
            continue
        try:
            import yaml as _yaml

            data = _yaml.safe_load(yaml_content)
        except Exception:
            return None
        if isinstance(data, dict) and data.get("schema_version") == "ANCHOR_SCOPE_REFRAME_V1":
            return data
    return None


_FENCE_OPEN_RE = re.compile(
    r"^(?P<prefix>(?:[ \t]*>)*[ \t]{0,8})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*)$"
)

_ANCHOR_SCOPE_REFRAME_KEY_NAMES = ("target", "decision", "allowed_path_deltas")


def _anchor_scope_reframe_fence_content_related(content_text: str) -> bool:
    """PR #2171 fix_delta (P2, OWNER adversarial review): a ```yaml fence's
    content only counts as an ANCHOR_SCOPE_REFRAME_V1 *attempt* -- not any
    unrelated fenced yaml a reviewer happens to quote (e.g. a config
    snippet) -- when it plausibly targets this schema.

    Fail-closed on ambiguity: content that does not even parse as YAML
    cannot be proven unrelated, so it is treated as related (the
    pre-existing present-but-invalid / fail_closed behavior for malformed
    fences, #2156 AC3, is preserved unchanged).
    """
    if "ANCHOR_SCOPE_REFRAME_V1" in content_text:
        return True
    try:
        import yaml as _yaml

        parsed = _yaml.safe_load(content_text)
    except Exception:
        return True
    if not isinstance(parsed, dict):
        return False
    if "schema_version" in parsed:
        return True
    return len(set(_ANCHOR_SCOPE_REFRAME_KEY_NAMES) & set(parsed.keys())) >= 2


def _anchor_scope_reframe_fence_present(comment_body: str) -> bool:
    """#2156 AC1 / PR #2171 fix_delta (P0-2, OWNER adversarial review):
    detect whether the comment body contains a ```yaml (or ~~~yaml) fence
    that is plausibly an ANCHOR_SCOPE_REFRAME_V1 payload attempt,
    independent of whether the fence's content parses as a valid
    ANCHOR_SCOPE_REFRAME_V1 payload.

    Used only to distinguish genuine absence (no such fence anywhere in the
    body -- the legitimate freeform lane) from present-but-invalid (a fence
    exists but is malformed YAML, declares the wrong schema_version, or is
    structurally rejected such as a blockquote-embedded fence). Non-canonical
    fence variants (blockquoted, malformed, wrong schema_version) must all
    stay in the present-but-invalid / fail_closed bucket -- only a body with
    no such fence marker at all is genuine absence.

    This is a line-oriented internal scanner (repo policy: PyYAML +
    jsonschema only, no external Markdown parser dependency added), not a
    full CommonMark implementation. It handles, unlike a single top-level
    regex: backtick fences of length >=3 (including 4+), tilde fences
    (``~~~yaml``), opening/closing fence length matching, 0-8 space /
    blockquote-marker indentation (including nested blockquotes), fences
    left unclosed through EOF, and a case-insensitive ``yaml`` info string.
    Presence is further gated on content relatedness (P2) via
    `_anchor_scope_reframe_fence_content_related()` so an unrelated ```yaml
    fence is not misclassified as a reframe attempt.
    """
    lines = comment_body.splitlines()
    n = len(lines)
    idx = 0
    found_any_yaml_fence = False
    found_related = False

    while idx < n:
        match = _FENCE_OPEN_RE.match(lines[idx])
        if not match:
            idx += 1
            continue
        info_tokens = match.group("info").strip().split()
        info_first_token = info_tokens[0].lower() if info_tokens else ""
        if info_first_token != "yaml":
            idx += 1
            continue

        fence_char = match.group("fence")[0]
        fence_len = len(match.group("fence"))
        prefix = match.group("prefix")
        close_re = re.compile(
            r"^(?:[ \t]*>)*[ \t]{0,8}"
            + re.escape(fence_char)
            + "{"
            + str(fence_len)
            + ",}[ \t]*$"
        )

        content_lines: list[str] = []
        close_idx = None
        j = idx + 1
        while j < n:
            if close_re.match(lines[j]):
                close_idx = j
                break
            content_lines.append(lines[j])
            j += 1

        found_any_yaml_fence = True

        if prefix.strip(" \t"):
            content_lines = [
                re.sub(r"^(?:[ \t]*>)+[ \t]?", "", line) for line in content_lines
            ]
        content_text = "\n".join(content_lines)

        if _anchor_scope_reframe_fence_content_related(content_text):
            found_related = True

        idx = (close_idx + 1) if close_idx is not None else n

    if not found_any_yaml_fence:
        return False
    return found_related



_TERMINAL_CLOSE_NOT_PLANNED_MARKER_RE = re.compile(
    r"^Decision:\s*CLOSE\s*/\s*NOT_PLANNED\s*$", re.IGNORECASE
)


def _has_terminal_close_not_planned_marker(anchor_body: str) -> bool:
    """Recognize only a final, non-quoted, non-fenced close disposition."""
    if not isinstance(anchor_body, str):
        return False

    terminal_lines: list[str] = []
    in_fence: tuple[str, int] | None = None
    fence_re = re.compile(r"^ {0,3}(`{3,}|~{3,})(?:[^`~]*)$")
    for raw_line in anchor_body.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith(">"):
            continue
        fence_match = fence_re.match(raw_line)
        if in_fence is not None:
            if fence_match and fence_match.group(1)[0] == in_fence[0] and len(fence_match.group(1)) >= in_fence[1]:
                in_fence = None
            continue
        if fence_match:
            fence = fence_match.group(1)
            in_fence = (fence[0], len(fence))
            continue
        if raw_line.strip():
            terminal_lines.append(raw_line.strip())

    return bool(terminal_lines) and bool(
        _TERMINAL_CLOSE_NOT_PLANNED_MARKER_RE.fullmatch(terminal_lines[-1])
    )


def _approved_close_not_planned_decision(
    *, author_assoc: str, anchor_url: str, anchor_hash: str, required_rerun: list | None = None
) -> dict:
    return {
        "status": "approved_by_trusted_anchor",
        "decision": "close_not_planned",
        "authorized_mutation_category": "not_planned",
        "implementation_go": False,
        "anchor_author_association": author_assoc,
        "anchor_comment_url": anchor_url,
        "anchor_comment_hash": anchor_hash,
        "required_rerun": required_rerun or [],
    }


def _is_approved_close_not_planned_decision(decision: "dict | None") -> bool:
    return (
        isinstance(decision, dict)
        and decision.get("status") == "approved_by_trusted_anchor"
        and decision.get("decision") == "close_not_planned"
        and decision.get("authorized_mutation_category") == "not_planned"
        and decision.get("anchor_author_association") == "OWNER"
        and decision.get("implementation_go") is False
    )

def _classify_anchor_scope_reframe(
    *,
    comment_payload: "dict",
    anchor_body: str,
    repo: str,
    issue_number: int,
    anchor_url: str,
) -> dict:
    """
    Classify anchor comment for ANCHOR_SCOPE_REFRAME_V1 trust and generate scope_delta_decision.

    Trusted if ALL of:
    - author_association in TRUSTED_ANCHOR_ASSOCIATIONS
    - Payload has ANCHOR_SCOPE_REFRAME_V1 schema_version
    - target.repo == repo
    - target.issue_number == issue_number
    - Payload passes anchor_scope_reframe_v1.schema.json validation

    Always returns a scope_delta_decision dict.
    """
    import hashlib as _hashlib

    author_assoc = comment_payload.get("author_association", "")
    anchor_hash = _hashlib.sha256(
        anchor_body.encode("utf-8") if isinstance(anchor_body, str) else anchor_body
    ).hexdigest()

    # Check author trust
    if author_assoc not in TRUSTED_ANCHOR_ASSOCIATIONS:
        return {
            "status": "fail_closed",
            "reason": f"untrusted_author_association: {author_assoc!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc or None,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Parse ANCHOR_SCOPE_REFRAME_V1 payload from body
    payload = _parse_anchor_scope_reframe_body(anchor_body)
    if payload is None:
        if _has_terminal_close_not_planned_marker(anchor_body):
            if author_assoc == "OWNER":
                return _approved_close_not_planned_decision(
                    author_assoc=author_assoc,
                    anchor_url=anchor_url,
                    anchor_hash=anchor_hash,
                )
            return {
                "status": "fail_closed",
                "reason": "close_not_planned_requires_owner",
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }
        # #2156 AC1-AC3: distinguish genuine absence (no ```yaml fence at
        # all -- the legitimate freeform lane, #2053/#2086) from
        # present-but-invalid (a fence exists but is malformed YAML,
        # declares the wrong schema_version, or is structurally rejected
        # such as a blockquote-embedded fence). Only genuine absence is
        # downgraded to `not_applicable`; present-but-invalid stays
        # `fail_closed` with a `schema_invalid:` reason so
        # `_structured_anchor_payload_present_but_invalid()` continues to
        # forbid the freeform-authority downgrade fallback for it.
        if _anchor_scope_reframe_fence_present(anchor_body):
            return {
                "status": "fail_closed",
                "reason": (
                    "schema_invalid: "
                    "anchor_scope_reframe_v1_fence_present_but_unparseable_or_wrong_schema_version"
                ),
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }
        return {
            "status": "not_applicable",
            "reason": "no_anchor_scope_reframe_v1_payload",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Validate against schema (fail-closed on schema error)
    schema = _load_schema("anchor_scope_reframe_v1.schema.json")
    if schema is not None:
        valid, errors = _validate_with_schema(payload, schema)
        if not valid:
            return {
                "status": "fail_closed",
                "reason": f"schema_invalid: {errors[:3]}",
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }

    # Check target.repo
    target = payload.get("target", {})
    if target.get("repo") != repo:
        return {
            "status": "fail_closed",
            "reason": f"wrong_repo: expected {repo!r}, got {target.get('repo')!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Check target.issue_number
    if target.get("issue_number") != issue_number:
        return {
            "status": "fail_closed",
            "reason": f"wrong_issue_number: expected {issue_number}, got {target.get('issue_number')!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # #2053 P2 fix-delta (iteration 3, OWNER PR review): a structured
    # ANCHOR_SCOPE_REFRAME_V1 anchor whose source generation/body revision
    # no longer matches current state is genuinely STALE -- distinct from
    # schema_invalid (never well-formed) or wrong_issue_number/wrong_repo
    # (well-formed but aimed elsewhere). GitHub comment metadata carries
    # `created_at`/`updated_at`; when both are present and differ, the
    # comment body actually consumed above (`anchor_body`) no longer
    # represents the original, unedited revision the payload was authored
    # against -- treat it as stale rather than silently trusting an edited
    # comment's current text as if it were the original approval.
    created_at = comment_payload.get("created_at")
    updated_at = comment_payload.get("updated_at")
    if (
        isinstance(created_at, str)
        and isinstance(updated_at, str)
        and created_at
        and updated_at
        and created_at != updated_at
    ):
        return {
            "status": "fail_closed",
            "reason": f"stale: comment edited after creation (created_at={created_at!r}, updated_at={updated_at!r})",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # close-only termination has a stricter trust boundary than a scope delta.
    if payload.get("decision") == "close_not_planned":
        if author_assoc != "OWNER":
            return {
                "status": "fail_closed",
                "reason": "close_not_planned_requires_owner",
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }
        return _approved_close_not_planned_decision(
            author_assoc=author_assoc,
            anchor_url=anchor_url,
            anchor_hash=anchor_hash,
            required_rerun=payload.get("required_rerun", []),
        )

    # All checks pass — trusted scope-delta anchor.
    return {
        "status": "approved_by_trusted_anchor",
        "decision": "approve_scope_delta",
        "implementation_go": False,
        "anchor_author_association": author_assoc,
        "anchor_comment_url": anchor_url,
        "anchor_comment_hash": anchor_hash,
        "allowed_path_deltas": payload.get("allowed_path_deltas", []),
        "required_rerun": payload.get("required_rerun", []),
    }


def _build_anchor_comment_state(
    *,
    anchor_url: str,
    comment: dict[str, Any],
    issue_number: int,
    captured_at: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    issue_url = comment.get("issue_url")
    if not isinstance(issue_url, str) or not issue_url:
        return None, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    parsed_url = urlparse(issue_url)
    path_parts = [part for part in parsed_url.path.split("/") if part]
    if len(path_parts) < 4 or path_parts[-2] != "issues" or path_parts[-1] != str(issue_number):
        return None, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    state = {
        "url": anchor_url,
        "id": comment.get("id"),
        "issue_number": issue_number,
        "html_url": comment.get("html_url"),
        "api_url": comment.get("url"),
        "user_login": ((comment.get("user") or {}).get("login")),
        "author_association": comment.get("author_association"),
        "snapshot": comment.get("body", ""),
        "captured_at": captured_at,
        "fetched_at": captured_at,
        "comment_created_at": comment.get("created_at"),
        "comment_updated_at": comment.get("updated_at"),
        "preliminary_classification": "feedback_update_required",
        "final_classification": None,
        (
            "classification_reason"
        ): "defaulted_by_preflight_schema_normalization; semantic classification deferred to #1008/#1011",
        "verified_claims": [],
        "unresolved_claims": [],
        "scope_impact": None,
        "requires_fact_check": False,
    }

    schema = _load_anchor_comment_schema()
    valid, errors = _validate_with_schema(state, schema)
    if not valid:
        return None, [BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID, *errors]
    return state, []


# ---------------------------------------------------------------------------
# Anchor comment structural validation
# ---------------------------------------------------------------------------


def _lookup_comment_in_fetched_list(comments: list[dict], comment_id: "str | int") -> Optional[dict]:
    """Issue #2052 fix_delta A: the actual resolution work of finding a
    single already-known ``comment_id`` inside an already-fetched,
    in-memory comments list -- the complete paginated traversal
    ``run_preflight()`` performs once via ``_fetch_issue_comments()``
    (Issue #2257 P0-1). Factored into a single named function (instead of
    being inlined separately at each call site) so both
    ``_validate_anchor_comment_url()``'s structural check and
    ``run_preflight()``'s own post-batch-validation resolution route this
    exact per-``comment_id`` lookup through ``_resolve_anchor_comment_payload``
    below -- the site that was previously duplicating this scan twice per
    invocation whenever an anchor comment URL was supplied."""
    for c in comments:
        if isinstance(c, dict) and str(c.get("id")) == str(comment_id):
            return c
    return None


def _resolve_anchor_comment_payload(
    *,
    comment_id: "str | int",
    repo: str,
    fixture_comments: Optional[list[dict]],
    transport: "object | None" = None,
    evidence_index: "object | None" = None,
) -> "tuple[Optional[dict], str]":
    """Resolve a single anchor comment's payload for ``comment_id``, either
    from an already-fetched in-memory ``fixture_comments`` list (the normal
    live-mode path, Issue #2257 P0-1) or, only when that list is not
    available (the fresh-readback/TOCTOU fallback), via a single-comment
    GET (``_fetch_single_comment``).

    Issue #2052 fix_delta A: this is the ONE place both ``run_preflight()``
    call sites that need this exact ``comment_id`` within a single
    invocation -- ``_validate_anchor_comment_url()``'s structural check and
    ``run_preflight()``'s own post-batch-validation re-resolution --
    perform their lookup/fetch. When an ``evidence_index`` is supplied
    (``evidence_cache_enabled=True``), the SECOND of those two call sites
    reuses the first call's result (same phase, same ``resource_id``)
    instead of repeating an identical list scan / GET -- this is the actual
    duplicate consumer operation eliminated by this fix_delta (previously
    ``evidence_index`` was only ever wired into the rare fallback branch,
    never into this normal, always-exercised-when-an-anchor-is-present
    path).

    Returns ``(comment_data, err)`` using the same ``err`` convention as
    ``_fetch_single_comment`` (empty string / falsy on success). A
    not-found result from the in-memory list branch returns a non-empty,
    module-internal sentinel ``err`` string that is deliberately never one
    of ``_fetch_single_comment``'s own semantic_missing/transport_failure
    prefixes -- callers on that branch must (and, per their pre-existing
    behavior below, do) decide "not found" purely from ``comment_data is
    None``, never from ``err`` content, since only the live-fetch branch's
    ``err`` is meaningfully classified by ``_is_transport_failure()``.
    """

    if fixture_comments is not None:
        def _fetch_fn() -> "tuple[Any, str]":
            found = _lookup_comment_in_fetched_list(fixture_comments, comment_id)
            return (found, "") if found is not None else (None, "not_found_in_prefetched_comments_list")
    else:
        def _fetch_fn() -> "tuple[Any, str]":
            return _fetch_single_comment(repo, comment_id, transport=transport)

    if evidence_index is not None:
        outcome = evidence_index.get_or_fetch(
            repository=repo,
            resource_kind=_EVIDENCE_RESOURCE_KIND_COMMENT,
            resource_id=comment_id,
            fetch_fn=_fetch_fn,
        )
        return outcome.raw_snapshot, (outcome.err or "")
    return _fetch_fn()


def _validate_anchor_comment_url(
    url: str,
    repo: str,
    issue_number: int,
    fixture_comments: Optional[list[dict]] = None,
    transport: "object | None" = None,
    evidence_index: "object | None" = None,
) -> tuple[bool, list[str]]:
    """
    Validate a single anchor comment URL structurally.

    Checks (all must pass):
    1. URL owner/repo matches --repo
    2. URL issue_number matches --issue-number
    3. Comment id exists (via gh api or fixture)
    4. Comment's issue_url REST field points to same issue (must be present and non-empty)
    5. Not a PR review comment (different endpoint)

    Returns (is_valid, list_of_blocker_codes).
    Uses structural URL parsing only — no substring checks.
    """
    parsed = _parse_anchor_comment_url(url)

    if not parsed.get("valid"):
        error = parsed.get("error", "unknown")
        if error == "pr_review_comment_url":
            return False, [BLOCKER_ANCHOR_IS_PR_REVIEW, BLOCKER_ANCHOR_NOT_IN_ISSUE]
        return False, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 1: owner/repo match
    url_owner = parsed["owner"].lower()
    url_repo_name = parsed["repo"].lower()
    parts = repo.lower().split("/", 1)
    if len(parts) != 2:
        return False, [BLOCKER_ANCHOR_REPO_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    expected_owner, expected_repo_name = parts
    if url_owner != expected_owner or url_repo_name != expected_repo_name:
        return False, [BLOCKER_ANCHOR_REPO_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 2: issue number match
    if parsed["issue_number"] != issue_number:
        return False, [BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    comment_id = parsed["comment_id"]

    # Check 3 & 4: comment exists and issue_url field matches.
    #
    # Issue #2052 fix_delta A: both the `fixture_comments is not None`
    # branch (the NORMAL live-mode path -- `run_preflight()` always passes
    # its complete paginated comments traversal here per Issue #2257 P0-1)
    # and the `fixture_comments is None` branch (the rare
    # fresh-readback/TOCTOU fallback) now route through the SAME
    # `_resolve_anchor_comment_payload()` helper, which itself routes
    # through `evidence_index` (when supplied) either way. This is what
    # makes the reuse actually observable in normal operation: previously
    # only the rarely-reached fallback branch was wired to
    # `evidence_index`, so a normal run (anchor comment already present in
    # the paginated list) never produced a single cache hit.
    comment_data, err = _resolve_anchor_comment_payload(
        comment_id=comment_id,
        repo=repo,
        fixture_comments=fixture_comments,
        transport=transport,
        evidence_index=evidence_index,
    )
    if comment_data is None:
        if fixture_comments is not None:
            # Fixture-list branch: absence here means the comment id was
            # never in the already-fetched list -- always
            # BLOCKER_ANCHOR_COMMENT_NOT_FOUND (pre-existing behavior,
            # unchanged; `err` is an internal sentinel, never a
            # transport/semantic classification here).
            return False, [BLOCKER_ANCHOR_COMMENT_NOT_FOUND, BLOCKER_ANCHOR_NOT_IN_ISSUE]
        # Live single-comment GET branch (Issue #2257 AC2/AC3): only a
        # genuine semantic_missing (true 404) is reported as
        # BLOCKER_ANCHOR_COMMENT_NOT_FOUND; a transport_failure must never
        # be misreported as "comment not found".
        if _is_transport_failure(err):
            reason_code = (
                err[len(_TRANSPORT_FAILURE_PREFIX):]
                if err.startswith(_TRANSPORT_FAILURE_PREFIX)
                else "transport_internal_error"
            )
            return False, [BLOCKER_ANCHOR_FETCH_TRANSPORT_FAILURE, f"ANCHOR_FETCH_REASON:{reason_code}"]
        return False, [BLOCKER_ANCHOR_COMMENT_NOT_FOUND, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 4: issue_url field validation — must be present and non-empty
    issue_url_field = comment_data.get("issue_url")

    # Missing or empty issue_url → blocked (fail-closed)
    if not issue_url_field:
        return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Expected format: https://api.github.com/repos/<owner>/<repo>/issues/<number>
    # Also accept: https://github.com/<owner>/<repo>/issues/<number>
    expected_api_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    expected_html_url = f"https://github.com/{repo}/issues/{issue_number}"

    if issue_url_field in (expected_api_url, expected_html_url):
        return True, []

    # Structural check via urlparse (not substring)
    parsed_url = urlparse(issue_url_field)
    path_parts = parsed_url.path.rstrip("/").split("/")
    if len(path_parts) >= 4 and path_parts[-2] == "issues" and path_parts[-1] == str(issue_number):
        # Repo path should be /<owner>/<repo>/
        if (
            len(path_parts) >= 5
            and path_parts[-4].lower() == expected_owner
            and path_parts[-3].lower() == expected_repo_name
        ):
            return True, []
        else:
            return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]
    else:
        return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]


def _validate_anchor_comments_batch(
    anchor_comment_urls: list[str],
    repo: str,
    issue_number: int,
    fixture_comments: Optional[list[dict]] = None,
    transport: "object | None" = None,
    evidence_index: "object | None" = None,
) -> tuple[list[str], list[str]]:
    """
    Validate all anchor comment URLs. Returns (stable_sorted_unique_valid_urls, all_blockers).

    Stable sort + dedupe per spec. One invalid URL blocks all.
    ANCHOR_NOT_IN_ISSUE is always included as canonical blocker when any URL fails.
    """
    if not anchor_comment_urls:
        return [], []

    all_blockers: list[str] = []
    seen_urls: set[str] = set()
    deduped_urls: list[str] = []

    for url in anchor_comment_urls:
        if url not in seen_urls:
            seen_urls.add(url)
            deduped_urls.append(url)

    # Stable sort
    sorted_urls = sorted(deduped_urls)
    if len(sorted_urls) > 1:
        return [], [BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED]

    for url in sorted_urls:
        valid, blockers = _validate_anchor_comment_url(
            url, repo, issue_number, fixture_comments=fixture_comments, transport=transport,
            evidence_index=evidence_index,
        )
        if not valid:
            all_blockers.extend(blockers)

    # Deduplicate blockers while preserving order
    seen_b: set[str] = set()
    deduped_blockers: list[str] = []
    for b in all_blockers:
        if b not in seen_b:
            seen_b.add(b)
            deduped_blockers.append(b)

    return sorted_urls, deduped_blockers


# ---------------------------------------------------------------------------
# Planner invocation
# ---------------------------------------------------------------------------


def _load_scope_rollup_artifact(repo_root: Path, issue_number: int) -> Optional[dict]:
    """
    Load a previously-persisted ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact for this
    Issue, if one exists (#1677 AC4 join). Rerunning plan_issue_scope_rollup.py
    itself is out of scope here (#1677 Out of Scope); this function only
    consumes an artifact that a prior preflight step already produced.

    Returns None (non-blocking) when no artifact is present or it fails to
    parse -- absence of scope-rollup evidence must not block the refinement
    preflight, it only means the planner falls back to a minimal
    ISSUE_EXECUTION_DECISION_V1 ('selected', no relations).
    """
    artifact_path = _issue_artifact_dir(repo_root, issue_number) / "issue_scope_rollup_plan_v2.json"
    if not artifact_path.exists():
        return None
    try:
        return json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _join_scope_rollup_into_planner_input(
    planner_input: dict[str, Any],
    scope_rollup_plan: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """
    Join an ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact into planner_input's
    known_context.scope_rollup_result (#1677 AC4).

    plan_refinement_loop.py's build_issue_execution_decision() reads
    known_context['scope_rollup_result'] to derive ISSUE_EXECUTION_DECISION_V1
    relations/execution state. Without this join, the planner always emits
    the minimal 'selected' shape regardless of known collisions.

    Pure function: does not mutate the input dict in place.
    """
    if not scope_rollup_plan:
        return planner_input
    joined = dict(planner_input)
    known_context = dict(joined.get("known_context") or {})
    known_context["scope_rollup_result"] = scope_rollup_plan
    joined["known_context"] = known_context
    return joined


def _build_planner_input(
    issue: dict,
    comments: list[dict],
    known_context: Optional[dict],
    anchor_comment_feedback: Optional[dict] = None,
    anchor_comment_ids: Optional[set[str]] = None,
    now: Optional[str] = None,
) -> dict:
    """Build REFINEMENT_LOOP_PLANNER_INPUT_V1 from issue/comments data."""
    labels = []
    raw_labels = issue.get("labels", [])
    for lbl in raw_labels:
        if isinstance(lbl, dict):
            labels.append(lbl.get("name", ""))
        elif isinstance(lbl, str):
            labels.append(lbl)

    planner_comments = comments
    if anchor_comment_ids:
        planner_comments = []
        for comment in comments:
            comment_id = comment.get("id")
            if comment_id is not None and str(comment_id) in anchor_comment_ids:
                sanitized = dict(comment)
                sanitized["body"] = "[redacted: anchor comment snapshot stored in artifact]"
                planner_comments.append(sanitized)
            else:
                planner_comments.append(comment)

    planner_input: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_PLANNER_INPUT,
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "labels": labels,
        },
        "comments": planner_comments,
    }
    if known_context is not None:
        planner_input["known_context"] = known_context
    if anchor_comment_feedback is not None:
        planner_input["anchor_comment_feedback"] = anchor_comment_feedback
    if now is not None:
        planner_input["now"] = now

    return planner_input


def _issue_body_source_ref(issue: dict[str, Any], issue_number: int, repo: str) -> str:
    html_url = issue.get("html_url")
    if isinstance(html_url, str) and html_url:
        return html_url
    url = issue.get("url")
    if isinstance(url, str) and url:
        return url
    return f"https://github.com/{repo}/issues/{issue_number}"


def _issue_artifact_dir(repo_root: Path, issue_number: int) -> Path:
    return repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)


def _snapshot_archive_dir(repo_root: Path, issue_number: int) -> Path:
    return _issue_artifact_dir(repo_root, issue_number) / "snapshots"


def _snapshot_body(snapshot: dict[str, Any]) -> str:
    issue = snapshot.get("issue")
    if not isinstance(issue, dict):
        return ""
    body = issue.get("body", "")
    return body if isinstance(body, str) else ""


def _snapshot_fetched_at(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("fetched_at", "")
    return value if isinstance(value, str) else ""


def _snapshot_archive_path(repo_root: Path, issue_number: int, snapshot: dict[str, Any]) -> Path:
    fetched_at = _snapshot_fetched_at(snapshot) or "unknown"
    safe_fetched_at = re.sub(r"[^0-9A-Za-z_-]+", "_", fetched_at)
    body_sha = _sha256(_snapshot_body(snapshot))
    return _snapshot_archive_dir(repo_root, issue_number) / (f"raw_issue_snapshot_{safe_fetched_at}_{body_sha}.json")


def _snapshot_source_ref(snapshot_path: Path, snapshot: dict[str, Any]) -> str:
    repo = snapshot.get("repo")
    issue_number = snapshot.get("issue_number")
    body_sha = _sha256(_snapshot_body(snapshot))
    fetched_at = _snapshot_fetched_at(snapshot)
    return (
        f"artifact:{snapshot_path.resolve()}"
        f"#repo={repo}&issue_number={issue_number}"
        f"&body_sha256={body_sha}&fetched_at={fetched_at}"
    )


def _load_valid_issue_snapshot(path: Path, issue_number: int, repo: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != "raw_issue_snapshot/v1":
        return None
    if payload.get("issue_number") != issue_number or payload.get("repo") != repo:
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("body"), str):
        return None
    return payload


def _materialize_immutable_snapshot(
    repo_root: Path,
    issue_number: int,
    snapshot: dict[str, Any],
) -> Path:
    archive_dir = _snapshot_archive_dir(repo_root, issue_number)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = _snapshot_archive_path(repo_root, issue_number, snapshot)
    if not archive_path.exists():
        archive_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    return archive_path


def _find_previous_immutable_snapshot(
    repo_root: Path,
    issue_number: int,
    repo: str,
    current_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    archive_dir = _snapshot_archive_dir(repo_root, issue_number)
    candidates: list[tuple[datetime, dict[str, Any], Path]] = []
    current_fetched_at = _snapshot_fetched_at(current_snapshot)
    try:
        current_sort_key = datetime.fromisoformat(current_fetched_at.replace("Z", "+00:00"))
    except ValueError:
        current_sort_key = None
    if archive_dir.exists():
        for path in archive_dir.glob("raw_issue_snapshot_*.json"):
            snapshot = _load_valid_issue_snapshot(path, issue_number, repo)
            if snapshot is None:
                continue
            fetched_at = _snapshot_fetched_at(snapshot)
            try:
                sort_key = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            except ValueError:
                sort_key = datetime.fromtimestamp(0, tz=timezone.utc)
            if current_sort_key is not None and sort_key >= current_sort_key:
                continue
            candidates.append((sort_key, snapshot, path))

    if not candidates:
        legacy_path = _issue_artifact_dir(repo_root, issue_number) / "raw_issue_snapshot.json"
        snapshot = _load_valid_issue_snapshot(legacy_path, issue_number, repo)
        if snapshot is not None:
            path = _materialize_immutable_snapshot(repo_root, issue_number, snapshot)
            candidates.append(
                (
                    datetime.fromisoformat(_snapshot_fetched_at(snapshot).replace("Z", "+00:00"))
                    if _snapshot_fetched_at(snapshot)
                    else datetime.fromtimestamp(0, tz=timezone.utc),
                    snapshot,
                    path,
                )
            )

    if not candidates:
        return None

    _, snapshot, path = max(candidates, key=lambda item: item[0])
    return snapshot, _snapshot_source_ref(path, snapshot)


def _ensure_scope_signal_delta_input(
    *,
    repo_root: Path,
    issue: dict[str, Any],
    raw_snapshot: dict[str, Any],
    known_context: Optional[dict[str, Any]],
    issue_number: int,
    repo: str,
) -> dict[str, Any]:
    merged = dict(known_context) if known_context else {}
    merged.setdefault("current_phase", "preflight")
    if "scope_signal_delta_input" in merged:
        return merged

    previous_snapshot = _find_previous_immutable_snapshot(
        repo_root,
        issue_number,
        repo,
        raw_snapshot,
    )
    if previous_snapshot is None:
        return merged

    previous_raw_snapshot, previous_source_ref = previous_snapshot
    current_body = issue.get("body", "") or ""
    current_archive_path = _snapshot_archive_path(repo_root, issue_number, raw_snapshot)
    current_source_ref = _snapshot_source_ref(current_archive_path, raw_snapshot)
    merged["scope_signal_delta_input"] = {
        "before_body": _snapshot_body(previous_raw_snapshot),
        "current_body": current_body,
        "after_body": current_body,
        "source_refs": {
            "before": previous_source_ref,
            "current": current_source_ref,
            "after": current_source_ref,
        },
    }
    return merged


def _validate_repair_result_schema_and_semantics(parsed: dict) -> Optional[str]:
    """Issue #2016 iteration-3 OWNER adversarial review, P0-1: validate a
    producer (`repair_issue_contract.py`) result against
    `repair_issue_contract_result_v1.schema.json` AND recompute
    `classify_repair_action()` from the raw `repairs[]` to cross-check the
    producer's self-reported `repair_action`. Returns None when the payload
    is fully valid (schema + cross-checks), else a fail-closed reason
    string. Applies to BOTH dry-run and --apply invocations.

    Fail-closed on:
      - schema violation (including additionalProperties: false)
      - missing/unknown `repair_action.schema_version` / `.policy_version`
        (never defaulted/backfilled -- Issue #2016 iteration-3 P0-1(2))
      - top-level SHA vs nested `repair_action` SHA mismatch (P0-1(4))
      - `changed` boolean disagreeing with SHA identity
      - `dry_run: true` with a non-null `candidate_body_artifact`
      - malformed `line_start`/`line_end` ranges
      - producer's self-reported `repair_action.disposition` /
        `.repair_kinds` disagreeing with `classify_repair_action()`
        recomputed from the raw `repairs[]` (P0-1(3))
    """
    schema = _load_schema("repair_issue_contract_result_v1.schema.json")
    if schema is None:
        return "repair_result_schema_unavailable"
    valid, errors = _validate_with_schema(parsed, schema)
    if not valid:
        return f"repair_result_schema_invalid:{errors[0] if errors else 'unknown'}"

    # anyOf(schema+error) payloads (CLI-level failure) have no repair_action
    # to cross-check; the caller already treats a present `error` key as an
    # environment_failure condition upstream of this validator.
    if parsed.get("error"):
        return None

    repair_action = parsed.get("repair_action")
    if not isinstance(repair_action, dict):
        return "repair_result_missing_repair_action"

    if repair_action.get("schema_version") != "repair_action/v1":
        return f"repair_action_schema_version_missing_or_unknown:{repair_action.get('schema_version')!r}"
    if repair_action.get("policy_version") != "deterministic-issue-repair/v1":
        return f"repair_action_policy_version_missing_or_unknown:{repair_action.get('policy_version')!r}"

    original_sha = parsed.get("original_body_sha256")
    repaired_sha = parsed.get("repaired_body_sha256")

    if repair_action.get("original_body_sha256") != original_sha:
        return "repair_action_nested_original_sha_mismatch"
    if repair_action.get("repaired_body_sha256") != repaired_sha:
        return "repair_action_nested_repaired_sha_mismatch"

    changed = parsed.get("changed")
    if changed is False and original_sha != repaired_sha:
        return "repair_changed_false_but_sha_mismatch"
    if changed is True and original_sha == repaired_sha:
        return "repair_changed_true_but_sha_identical"

    dry_run = parsed.get("dry_run")
    if dry_run is True and repair_action.get("candidate_body_artifact") is not None:
        return "repair_dry_run_but_candidate_artifact_present"

    repairs = parsed.get("repairs")
    if not isinstance(repairs, list):
        return "repair_repairs_field_not_a_list"

    for record in repairs:
        if not isinstance(record, dict):
            continue
        start = record.get("line_start")
        end = record.get("line_end")
        if isinstance(start, int) and isinstance(end, int):
            if start < 1 or end < 1 or start > end:
                return f"repair_record_invalid_line_range:{start}:{end}"

    if classify_repair_action is None:
        return "repair_action_classifier_unavailable"
    recomputed = classify_repair_action(original_sha, repaired_sha, repairs)
    if recomputed.get("disposition") != repair_action.get("disposition"):
        return (
            f"repair_action_disposition_mismatch:producer={repair_action.get('disposition')!r}"
            f":recomputed={recomputed.get('disposition')!r}"
        )
    if sorted(recomputed.get("repair_kinds", [])) != sorted(repair_action.get("repair_kinds", []) or []):
        return "repair_action_repair_kinds_mismatch"

    return None


def _run_repair_subprocess(argv_extra: list[str], body: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Run repair_issue_contract.py as a subprocess with `argv_extra` appended
    after `--body-file <tmp>`. Fail-closed (Issue #2016 P0-3): returncode,
    stdout presence, and JSON structure are all explicitly checked. Never
    raises. Returns (parsed_dict_or_None, error_reason_or_None) — exactly
    one of the two is non-None.
    """
    import tempfile
    import os as _os
    import sys as _sys
    import subprocess as _sp
    import json as _json

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        proc = _sp.run(
            [_sys.executable, str(REPAIR_SCRIPT), "--body-file", tmp_path, *argv_extra],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except _sp.TimeoutExpired:
        return None, "repair_subprocess_timeout"
    except Exception as exc:
        return None, f"repair_subprocess_launch_error:{type(exc).__name__}:{exc}"
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    # P0-3: explicitly check returncode. subprocess.run() defaults check=False,
    # so a non-zero exit with JSON-looking stdout would otherwise silently
    # pass through as a normal payload.
    if proc.returncode != 0:
        return None, f"repair_subprocess_nonzero_exit:{proc.returncode}:{(proc.stderr or '')[:500]}"

    if not proc.stdout:
        return None, "repair_subprocess_no_stdout"

    try:
        parsed = _json.loads(proc.stdout)
    except _json.JSONDecodeError as exc:
        return None, f"repair_subprocess_invalid_json:{exc}"

    if not isinstance(parsed, dict):
        return None, "repair_subprocess_payload_not_object"

    if parsed.get("schema") != "repair_issue_contract/v1":
        return None, f"repair_subprocess_schema_mismatch:{parsed.get('schema')!r}"

    if parsed.get("error"):
        return None, f"repair_subprocess_payload_error:{parsed.get('error')}"

    # Issue #2016 iteration-3 P0-1: validate schema + cross-check
    # classify_repair_action() BEFORE trusting the producer's self-reported
    # repair_action. A schema violation or a disposition/repair_kinds
    # mismatch is treated identically to a subprocess-level failure
    # (fail-closed -> environment_failure upstream).
    semantic_error = _validate_repair_result_schema_and_semantics(parsed)
    if semantic_error is not None:
        return None, f"repair_subprocess_semantic_validation_failed:{semantic_error}"

    return parsed, None


def _invoke_repair(body: str) -> dict:
    """
    Invoke repair_issue_contract.py (dry-run) to pre-process the Issue body
    before feeding it to the planner.

    Returns the repair result dict (schema: repair_issue_contract/v1). On any
    subprocess/JSON/schema failure (Issue #2016 P0-3, fail-closed), returns a
    dict carrying an "error" key; callers MUST treat a present "error" key as
    an environment_failure condition rather than as changed=False/no-op.
    """
    parsed, error = _run_repair_subprocess([], body)
    if error is not None:
        return {
            "schema": "repair_issue_contract/v1",
            "changed": False,
            "repairs": [],
            "error": error,
        }
    return parsed


def _write_planner_spawn_attempt_marker(
    repo_root: Optional[Path], issue_number: Optional[int], *, script_missing: bool
) -> None:
    """Issue #2073 P1-4 (OWNER review): record a pre-spawn diagnostic stage
    *before* the planner subprocess.run() call is issued, so a future
    reproduction of the `pid_proof_planner.json` flake can distinguish "the
    planner subprocess.run() call site was never reached" from "the call was
    made but the child never got far enough to write its own proof". This is
    intentionally the smallest possible monotonic stage marker (not a general
    telemetry system) and is best-effort: a failure to write it must never
    change `_invoke_planner`'s own control flow or return value.
    """
    if repo_root is None or issue_number is None:
        return
    try:
        marker_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "planner_spawn_attempt_v1.json").write_text(
            json.dumps(
                {
                    "schema_version": "PLANNER_SPAWN_ATTEMPT_V1",
                    "stage": "pre_subprocess_run",
                    "script_missing_precheck": script_missing,
                    "pid": os.getpid(),
                    "recorded_at": _now_iso(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _invoke_planner(
    planner_input: dict,
    *,
    repo_root: Optional[Path] = None,
    issue_number: Optional[int] = None,
) -> tuple[dict | None, int, str, str]:
    """
    Invoke plan_refinement_loop.py via subprocess.run([sys.executable, ...], shell=False).

    Returns (plan_dict, exit_code, stderr_text, raw_stdout).
    plan_dict is None on JSON parse failure.

    `repo_root` / `issue_number` are optional and, when supplied, are used
    only to write the best-effort `planner_spawn_attempt_v1.json` pre-spawn
    diagnostic marker (Issue #2073 P1-4); they never affect the planner
    invocation itself. The marker write is this function's literal first
    statement (Issue #2073 human-review P2-1: it previously ran after
    `json.dumps(planner_input, ...)`, so a `planner_input` serialization
    failure -- e.g. non-JSON-serializable content, or a NaN/Infinity value
    rejected by `allow_nan=False` -- would raise before the marker was ever
    written, reproducing the exact "absence of evidence is not evidence of
    absence" gap this marker exists to close).
    """
    script_missing = not PLANNER_SCRIPT.is_file()
    _write_planner_spawn_attempt_marker(repo_root, issue_number, script_missing=script_missing)

    input_json = json.dumps(planner_input, ensure_ascii=False, allow_nan=False)

    # Issue #2073 P1-2 (OWNER review): the previous implementation relied on
    # subprocess.run() raising FileNotFoundError to detect a missing planner
    # script, but that exception is only raised when the *executable*
    # (argv[0], i.e. sys.executable) itself cannot be found -- not when a
    # present interpreter is asked to run a missing *script* (argv[1]). In
    # that case the interpreter itself starts, prints its own "can't open
    # file" message to stderr, and exits with its own nonzero code (commonly
    # 2), which previously fell through to the generic JSONDecodeError path
    # below and was misclassified identically to an "invalid input / schema
    # error" (exit 2) planner failure. Detecting the missing script
    # explicitly, before ever spawning a process, makes this failure mode
    # deterministic and reuses the same (exit 3, "planner script not
    # found: ...") shape the `except FileNotFoundError` branch below already
    # produces for the (much rarer) missing-executable case, so downstream
    # classification (`classify_planner_failure`) sees a consistent
    # "not found" signal in `stderr` regardless of which of the two ways the
    # script turned out to be missing.
    if script_missing:
        return None, 3, f"planner script not found: {PLANNER_SCRIPT}", ""

    try:
        proc = subprocess.run(
            [sys.executable, str(PLANNER_SCRIPT)],
            input=input_json,
            shell=False,
            timeout=PLANNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None, 3, f"planner timeout after {PLANNER_TIMEOUT}s", ""
    except FileNotFoundError:
        return None, 3, f"planner script not found: {PLANNER_SCRIPT}", ""
    except Exception as exc:
        return None, 3, f"planner unexpected error: {exc}", ""

    stderr_text = proc.stderr or ""
    exit_code = proc.returncode
    raw_stdout = proc.stdout or ""

    if exit_code not in (0, 2, 3):
        return None, exit_code, stderr_text, raw_stdout

    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, exit_code, f"planner stdout JSON decode error: {exc}", raw_stdout

    return plan, exit_code, stderr_text, raw_stdout


# ---------------------------------------------------------------------------
# warn condition detection
# ---------------------------------------------------------------------------


def _has_unknown_confidence(plan: dict) -> bool:
    """
    Return True if any decision in plan.decisions.*.confidence == "unknown".

    This determines the warn condition:
    planner exit 0 + fail_closed.required == false + >=1 unknown confidence → warn/1.
    """
    decisions = plan.get("decisions", {})
    for _key, policy in decisions.items():
        if isinstance(policy, dict) and policy.get("confidence") == "unknown":
            return True
    return False


# ---------------------------------------------------------------------------
# Exit code mapping
# ---------------------------------------------------------------------------


def _apply_exit_code_mapping(
    planner_exit_code: Optional[int],
    planner_fail_closed: Optional[bool],
    blockers: list[str],
    plan: Optional[dict] = None,
    scope_delta_decision: Optional[dict] = None,
    repair_needs_fix: bool = False,
) -> tuple[str, int]:
    """
    Apply the Planner ↔ Wrapper Exit Code Mapping table.

    Returns (status_str, exit_code_int).

    warn condition: planner exit 0 AND fail_closed=false AND >=1 unknown confidence
    → status: warn / exit 1

    PR #1973 (OWNER REQUEST_CHANGES, P1-5): a genuine multi-turn
    trusted-owner advisory route (`scope_delta_decision.status == "warn"`,
    set by `_apply_multi_turn_candidate_route()`) must also surface as a
    CI-visible warn/EXIT_WARN, not silently stay `pass`/EXIT_PASS just
    because `_has_unknown_confidence(plan)` happens to be False.

    Issue #2016: `repair_needs_fix=True` (repair_action.disposition ==
    "auto_apply_safe", with no unrelated blocker present) overrides an
    otherwise pass/warn outcome to needs_fix/EXIT_NEEDS_FIX. It intentionally
    does NOT override blocked/environment_failure — those routes already
    indicate a more fundamental stop condition than a repairable Issue body
    defect. AC5 (Issue #2016): when `repair_needs_fix=False` (e.g. changed:
    false), this function's return value is unchanged from before Issue #2016.
    """
    # Pre-planner blockers (anchor mismatch, gh failure)
    if blockers:
        anchor_blockers = {
            BLOCKER_ANCHOR_NOT_IN_ISSUE,
            BLOCKER_ANCHOR_REPO_MISMATCH,
            BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH,
            BLOCKER_ANCHOR_COMMENT_NOT_FOUND,
            BLOCKER_ANCHOR_ISSUE_URL_MISMATCH,
            BLOCKER_ANCHOR_IS_PR_REVIEW,
            BLOCKER_INPUT_SCHEMA_INVALID,
            BLOCKER_INVALID_ARGS,
        }
        env_blockers = {
            BLOCKER_GH_FAILURE,
            BLOCKER_RESULT_SCHEMA_INVALID,
            BLOCKER_REPAIR_ENVIRONMENT_FAILURE,
        }
        has_env = any(b in env_blockers for b in blockers)
        has_anchor = any(b in anchor_blockers for b in blockers)
        # AC6: REWRITE_CONSTRAINTS_* and planner_fail_closed_payload_invalid
        # are environment failures (payload integrity), not issue blockers.
        rewrite_env_blockers = {
            BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD,
            BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE,
            BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION,
            BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID,
            BLOCKER_ARTIFACT_PROJECTION_MISMATCH,
        }
        if any(any(b.split(":", 1)[0] == rb for rb in rewrite_env_blockers) for b in blockers):
            has_env = True

        if has_env:
            return "environment_failure", EXIT_ENVIRONMENT_FAILURE
        if has_anchor:
            return "blocked", EXIT_BLOCKED
        # Other pre-planner blockers → blocked
        return "blocked", EXIT_BLOCKED

    if planner_exit_code is None:
        return "environment_failure", EXIT_ENVIRONMENT_FAILURE

    if planner_exit_code == 2:
        return "blocked", EXIT_BLOCKED

    if planner_exit_code == 3:
        return "environment_failure", EXIT_ENVIRONMENT_FAILURE

    if planner_exit_code == 0:
        if planner_fail_closed is True:
            return "blocked", EXIT_BLOCKED
        if repair_needs_fix:
            return "needs_fix", EXIT_NEEDS_FIX
        # Check warn condition: >=1 decision has confidence: unknown
        if plan is not None and _has_unknown_confidence(plan):
            return "warn", EXIT_WARN
        # Check warn condition: multi-turn trusted-owner advisory route
        if scope_delta_decision is not None and scope_delta_decision.get("status") == "warn":
            return "warn", EXIT_WARN
        return "pass", EXIT_PASS

    # Unknown exit code
    return "environment_failure", EXIT_ENVIRONMENT_FAILURE


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def _write_artifacts(
    repo_root: Path,
    issue_number: int,
    raw_snapshot: dict,
    planner_input: dict,
    result: dict,
) -> dict[str, str]:
    """
    Write artifacts to .claude/artifacts/issue-refinement-loop/<issue_number>/.

    Returns {artifact_key: absolute_path_str}.
    issue_number is int-normalized; path is NOT generated from repo name or URL.

    Writes:
      - raw_issue_snapshot.json  (raw issue + comments)
      - planner_input.json       (planner stdin JSON, byte-stable)
      - refinement_preflight_result_v1.json (canonical result)
    """
    artifact_dir = _issue_artifact_dir(repo_root, issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = artifact_dir / "raw_issue_snapshot.json"
    planner_input_path = artifact_dir / "planner_input.json"
    result_path = artifact_dir / "refinement_preflight_result_v1.json"
    artifacts = {
        "raw_issue_snapshot": str(snapshot_path),
        "planner_input": str(planner_input_path),
        "refinement_preflight_result_v1": str(result_path),
    }
    # Issue #2016 iteration-3 P1-1: needs_fix results additionally carry
    # `repair_diagnostics` / `repair_candidate_body` in artifacts. These are
    # written earlier in the flow (repair_diagnostics.json /
    # repaired_issue_body.md), so this validates presence + the expected
    # fixed on-disk path rather than re-writing them, and fails closed on
    # any unrecognized extra key or path drift.
    result_artifacts = result.get("artifacts")
    allowed_extra_artifact_keys = {
        "repair_diagnostics": artifact_dir / "repair_diagnostics.json",
        "repair_candidate_body": artifact_dir / "repaired_issue_body.md",
    }
    if isinstance(result_artifacts, dict):
        for key in set(result_artifacts.keys()) - set(artifacts.keys()):
            expected_path = allowed_extra_artifact_keys.get(key)
            if expected_path is None:
                raise ValueError(f"final_result_artifact_projection_mismatch: unexpected_key {key}")
            if result_artifacts.get(key) != str(expected_path):
                raise ValueError(f"final_result_artifact_projection_mismatch: path_drift {key}")
            if not expected_path.is_file():
                raise ValueError(f"final_result_artifact_projection_missing_file: {key}")
            artifacts[key] = result_artifacts[key]
    if result.get("artifacts") != artifacts:
        raise ValueError("final_result_artifact_projection_mismatch")
    schema_errors = _validate_result_artifact(result)
    if schema_errors:
        raise ValueError("final_result_schema_invalid: " + "; ".join(schema_errors))
    _atomic_write_json(snapshot_path, raw_snapshot)
    _materialize_immutable_snapshot(repo_root, issue_number, raw_snapshot)
    _atomic_write_json(planner_input_path, planner_input)
    _atomic_write_json(result_path, result)
    try:
        readback = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"result_artifact_readback_error:{type(exc).__name__}:{exc}") from exc
    readback_errors = _validate_result_artifact(readback)
    if readback_errors:
        raise ValueError("result_artifact_readback_schema_invalid: " + "; ".join(readback_errors))
    return artifacts


def _atomic_write_json(path: Path, value: Any) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    """Same atomicity/durability contract as _atomic_write_json(), for
    non-JSON text artifacts (e.g. candidate Issue body markdown)."""
    encoded = text.encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _materialize_auto_apply_candidate(
    body: str,
    dry_run_repair_result: dict,
    artifact_dir: Path,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Re-invoke repair_issue_contract.py with --apply against the SAME body
    that produced the dry-run auto_apply_safe classification, to materialize
    a candidate_body_artifact for the needs_fix / apply_deterministic_repair
    route (Issue #2016 recommended design, "auto-apply is an optimistic
    concurrency transaction"). This performs NO GitHub mutation — it only
    writes a local candidate file inside `artifact_dir`.

    Returns (apply_result_dict, None) on success, or (None, reason) on any
    fail-closed condition (subprocess failure, input SHA drift between the
    two invocations, artifact write/readback failure).
    """
    candidate_path = artifact_dir / "repaired_issue_body.md"

    parsed, error = _run_repair_subprocess(
        ["--apply", "--out-file", str(candidate_path), "--artifact-root", str(artifact_dir)], body
    )
    if error is not None:
        return None, f"repair_apply_{error}"

    # Optimistic concurrency guard: the dry-run and --apply invocations must
    # observe the identical input/output hash pair. A mismatch means the
    # body changed between the two subprocess calls (or a producer version
    # drift), and auto-apply must NOT proceed.
    if parsed.get("original_body_sha256") != dry_run_repair_result.get("original_body_sha256"):
        return None, "repair_apply_input_sha_mismatch"
    if parsed.get("repaired_body_sha256") != dry_run_repair_result.get("repaired_body_sha256"):
        return None, "repair_apply_output_sha_mismatch"

    if not candidate_path.exists():
        return None, "repair_apply_candidate_artifact_missing"

    try:
        written_text = candidate_path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"repair_apply_candidate_artifact_unreadable:{type(exc).__name__}"

    written_sha = "sha256:" + hashlib.sha256(written_text.encode("utf-8")).hexdigest()
    if written_sha != parsed.get("repaired_body_sha256"):
        return None, "repair_apply_candidate_artifact_readback_mismatch"

    return parsed, None


def _validate_result_artifact(result: Any) -> list[str]:
    schema = _load_schema("refinement_preflight_result_v1.schema.json")
    if schema is None:
        return ["result_schema_unavailable"]
    valid, errors = _validate_with_schema(result, schema)
    return [] if valid else errors


# ---------------------------------------------------------------------------
# Compact stdout projection
# ---------------------------------------------------------------------------


def _build_compact_stdout(result: dict) -> str:
    """
    Build agent-friendly compact projection of result for stdout.

    Canonical fields (always emitted if present):
      STATUS, NEXT_ACTION, MUST_READ, COMMANDS, BLOCKERS, ARTIFACT

    MUST NOT include raw issue body, raw comments, or any sentinel-containing fields.
    DO_NOT_READ is a reserved field and is intentionally not emitted here.
    EVIDENCE (raw body/comments) is never emitted to stdout.
    """
    lines = [
        f"STATUS: {result['status']}",
        f"NEXT_ACTION: {result['next_action']}",
    ]

    must_read = result.get("must_read", [])
    if must_read:
        lines.append("MUST_READ:")
        for p in must_read:
            lines.append(f"  - {p}")

    commands = result.get("commands", [])
    if commands:
        try:
            from command_registry import REGISTRY as _REG

            spec_objects = []
            for cmd in commands:
                cmd_id = cmd.get("id") or cmd.get("kind", "?")
                entry = _REG.get(cmd_id, {})
                spec_objects.append(
                    {
                        "id": cmd_id,
                        "argv": cmd.get("argv", []),
                        "shell": cmd.get("shell", False),
                        "cwd_policy": entry.get("cwd_policy", "repo_root"),
                        "stdin_contract": entry.get("stdin_contract", "none"),
                        "stdout_contract": entry.get("stdout_contract", "unknown"),
                        "timeout_seconds": entry.get("timeout_seconds", 120),
                        "mutation": entry.get("mutation", False),
                    }
                )
            lines.append("COMMANDS_JSON: " + json.dumps(spec_objects, ensure_ascii=False, separators=(",", ":")))
            lines.append("COMMANDS_DISPLAY:")
            for cmd in commands:
                argv_str = " ".join(str(a) for a in cmd.get("argv", []))
                lines.append(f"  display: [{cmd.get('id') or cmd.get('kind', '?')}] {argv_str}")
        except ImportError:
            lines.append("COMMANDS:")
            for cmd in commands:
                argv_str = " ".join(cmd.get("argv", []))
                lines.append(f"  - [{cmd.get('kind', '?')}] {argv_str}")

    blockers = result.get("blockers", [])
    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  - {b}")

    required_sections = result.get("required_sections", [])
    if required_sections:
        lines.append("REQUIRED_SECTIONS:")
        for section in required_sections:
            lines.append(f"  - {section}")

    required_contract_keys = result.get("required_contract_keys", [])
    if required_contract_keys:
        lines.append("REQUIRED_CONTRACT_KEYS:")
        for key in required_contract_keys:
            lines.append(f"  - {key}")

    rewrite_constraints = result.get("rewrite_constraints")
    if rewrite_constraints:
        lines.append("REWRITE_CONSTRAINTS:")
        rewritten = json.dumps(
            rewrite_constraints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        lines.append(f"  {rewritten}")

    repair_action = result.get("repair_action")
    if repair_action:
        lines.append("REPAIR_ACTION:")
        rewritten_repair_action = json.dumps(
            repair_action,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        lines.append(f"  {rewritten_repair_action}")

    contract_update = result.get("contract_update")
    if isinstance(contract_update, dict) and contract_update.get("status") == "failed":
        # The handoff projector already mapped arbitrary consumer messages to
        # a closed token; never print the raw anchor/body or failure payload.
        lines.append(f"CONTRACT_UPDATE_REASON_CODE: {contract_update['reason_code']}")

    if result["status"] == "environment_failure":
        lines.append(f"REASON_CODE: {result.get('reason_code')}")
        lines.append(f"SOURCE: {result.get('source')}")
        lines.append(f"OPERATION: {result.get('operation')}")

    artifacts = result.get("artifacts", {})
    if artifacts:
        lines.extend(_render_artifact_projection_lines(artifacts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def _build_result(
    *,
    status: str,
    issue_number: int,
    repo: str,
    planner_exit_code: Optional[int],
    planner_fail_closed: Optional[bool],
    next_action: str,
    must_read: list[str],
    do_not_read: list[str],
    commands: list[dict],
    blockers: list[str],
    planner_fail_closed_reason_codes: list[str],
    required_sections: list[str],
    required_contract_keys: list[str],
    rewrite_constraints: Optional[dict[str, Any]],
    artifacts: dict[str, str],
    hashes: dict[str, str],
    contract_update: Optional[dict[str, Any]] = None,
    repair_action: Optional[dict[str, Any]] = None,
    structural_repair_action: Optional[dict[str, Any]] = None,
    environment_failure_reason_code: Optional[str] = None,
    environment_failure_source: Optional[str] = None,
    environment_failure_operation: Optional[str] = None,
) -> dict:
    """Build a refinement_preflight_result/v1 compliant dict.

    Issue #2257 AC10: `environment_failure_reason_code`/`_source`/`_operation`
    populate the schema's `reason_code`/`source`/`operation` structured
    projection, and ONLY when `status == "environment_failure"` -- the
    schema's conditional contract makes these three fields required when
    `status == "environment_failure"` and forbidden otherwise, so this
    function silently drops them for any other status rather than ever
    emitting a stale/misleading projection alongside a non-failure status.
    """
    result = {
        "schema_version": SCHEMA_VERSION_RESULT,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "planner_exit_code": planner_exit_code,
        "planner_fail_closed": planner_fail_closed,
        "next_action": next_action,
        "must_read": must_read,
        "do_not_read": do_not_read,
        "commands": commands,
        "blockers": blockers,
        "planner_fail_closed_reason_codes": planner_fail_closed_reason_codes,
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "artifacts": artifacts,
        "hashes": hashes,
    }
    if rewrite_constraints is not None:
        result["rewrite_constraints"] = rewrite_constraints
    if contract_update is not None:
        result["contract_update"] = contract_update
    if repair_action is not None:
        result["repair_action"] = repair_action
    if structural_repair_action is not None:
        result["structural_repair_action"] = structural_repair_action
    if status == "environment_failure":
        # Issue #2257 AC10: the schema's conditional contract makes these
        # three fields REQUIRED whenever status == "environment_failure" --
        # so every such result must carry them, including call sites (some
        # outside this Issue's Allowed Paths, e.g. pre-existing tests that
        # call `_emit_failure_result`/`_build_result` directly) that do not
        # pass a precise `environment_failure_*` value. Falling back to the
        # fixed, closed-enum defaults below (rather than omitting the keys)
        # keeps every environment_failure result schema-valid; callers that
        # DO know the precise transport/operation (the credentialless read
        # path this Issue is about) still get the accurate, specific value.
        result["reason_code"] = environment_failure_reason_code or "transport_internal_error"
        result["source"] = environment_failure_source or "internal"
        result["operation"] = environment_failure_operation or "unspecified"
    return result



def _bounded_contract_update_gate(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unavailable"


_CONTRACT_UPDATE_PREFLIGHT_STATES = frozenset({
    "pass", "warn", "needs_fix", "blocked", "environment_failure", "unavailable", "not_run",
})
_CONTRACT_UPDATE_REVIEW_STATES = frozenset({"approve", "needs_fix", "unavailable", "not_run"})
_CONTRACT_UPDATE_READINESS_STATES = frozenset({
    "go", "human_judgment", "input_or_runtime_error", "unavailable", "not_run",
})


def _bounded_contract_update_handoff(consumer_result: dict[str, Any]) -> dict[str, Any]:
    """Project the transaction-local consumer result into the existing result.

    This intentionally retains only the phase outcome needed by the parent
    orchestrator.  Source bodies, candidate bodies, operation identities, and
    transaction payloads remain local to the controlled phase.

    PR #2057 OWNER REQUEST_CHANGES revision (P0-2 / P1-3): the returned dict
    now carries an explicit ``disposition`` (patch / proven_no_change /
    full_rewrite_required / invalid, echoing the single canonical
    classifier) alongside ``status``, and TWO status values that used to
    collapse into an existing enum member now have their own outcome:

    - An `invalid` disposition (malformed/binding-mismatched scope-delta
      decision) is `status: failed` with `disposition: invalid` -- distinct
      from an ordinary `no_change`/`proven_no_change` outcome, which it
      previously matched byte-for-byte.
    - A `full_rewrite_required` disposition (approved scope reframe, empty
      operations[], not yet reflected) is `status: handoff_required` --
      NOT `failed`. No write was ever attempted (`writes` stays 0); this is
      a legitimate control transfer to issue-editor, not a mutation
      failure. `final_readback` is `not_applicable` (nothing was written,
      so there is nothing to read back -- never a synthesized `verified`).
    """
    raw_status = consumer_result.get("status")
    states = consumer_result.get("states")
    state = states.get("contract_update", {}).get("status") if isinstance(states, dict) else None
    iterations = consumer_result.get("iterations", 0)
    fresh = consumer_result.get("fresh_checks")
    if not isinstance(fresh, dict):
        fresh = {}
    rewrite_route = consumer_result.get("rewrite_route")
    disposition_sidecar = consumer_result.get("disposition")

    if raw_status == "invalid" or (
        isinstance(disposition_sidecar, dict) and disposition_sidecar.get("disposition") == "invalid"
    ):
        raw_reason = disposition_sidecar.get("reason_code") if isinstance(disposition_sidecar, dict) else None
        reason_code = (
            raw_reason if isinstance(raw_reason, str) and raw_reason in CONTRACT_UPDATE_FAILED_REASONS
            else "invalid_scope_delta_decision"
        )
        return {
            "status": "failed",
            "disposition": "invalid",
            "writes": 0,
            "iterations": int(iterations) if isinstance(iterations, int) else 0,
            "final_readback": "not_applicable",
            "fresh_preflight": "unavailable",
            "fresh_review": "unavailable",
            "fresh_readiness": "unavailable",
            "reason_code": reason_code,
        }

    if isinstance(rewrite_route, dict) and rewrite_route.get("route") == "issue_editor_required":
        rewrite_reason = rewrite_route.get("reason_code")
        if not isinstance(rewrite_reason, str) or rewrite_reason not in {
            "approved_scope_requires_full_contract_rewrite",
            "explicit_trusted_human_directive_requires_issue_editor",
        }:
            return {
                "status": "failed", "disposition": "invalid", "writes": 0, "iterations": 0,
                "final_readback": "not_applicable", "fresh_preflight": "unavailable",
                "fresh_review": "unavailable", "fresh_readiness": "unavailable",
                "reason_code": "contract_update_failed",
            }
        return {
            "status": "handoff_required",
            "disposition": "full_rewrite_required",
            "writes": 0,
            "iterations": 0,
            "final_readback": "not_applicable",
            "fresh_preflight": _bounded_contract_update_gate(fresh.get("preflight"), _CONTRACT_UPDATE_PREFLIGHT_STATES),
            "fresh_review": _bounded_contract_update_gate(fresh.get("review"), _CONTRACT_UPDATE_REVIEW_STATES),
            "fresh_readiness": _bounded_contract_update_gate(fresh.get("readiness"), _CONTRACT_UPDATE_READINESS_STATES),
            "reason_code": rewrite_reason,
        }

    # A completed transaction is not an implementation authorization.  The
    # post-update rerun is a six-gate conjunction: missing, warn, or failing
    # checks must fail the phase even though the controlled write itself
    # reached final readback successfully.  Do not infer a pass from prose or
    # an earlier transaction result.
    post_update_gate_passed = (
        fresh.get("preflight") == "pass"
        and fresh.get("review") == "approve"
        and fresh.get("readiness") == "go"
        and fresh.get("allowed_paths") == "pass"
        and fresh.get("permission_profile") == "pass"
        and fresh.get("runtime_evidence") == "pass"
    )
    if raw_status == "applied" and iterations == 1 and post_update_gate_passed:
        status = "rebased"
    elif raw_status in {"applied", "no_change"} and post_update_gate_passed:
        status = state if state in {"applied", "no_change", "rebased"} else "failed"
    else:
        status = "failed"
    disposition = "patch" if raw_status == "applied" else ("proven_no_change" if raw_status == "no_change" else None)
    raw_failure = consumer_result.get("failure")
    reason_code = (
        raw_failure if isinstance(raw_failure, str) and raw_failure in CONTRACT_UPDATE_FAILED_REASONS
        else "post_update_gate_failed" if raw_status in {"applied", "no_change"}
        else "contract_update_failed"
    )
    writes = consumer_result.get("writes", 0)
    writes = writes if type(writes) is int and writes in {0, 1} else 0
    if raw_status in {"applied", "no_change"}:
        final_readback = "verified"
    else:
        final_readback = "not_applicable" if writes == 0 else "failed"
    return {
        "status": status,
        "disposition": disposition,
        "writes": writes,
        "iterations": int(iterations) if isinstance(iterations, int) else 0,
        "final_readback": final_readback,
        "fresh_preflight": _bounded_contract_update_gate(fresh.get("preflight"), _CONTRACT_UPDATE_PREFLIGHT_STATES),
        "fresh_review": _bounded_contract_update_gate(fresh.get("review"), _CONTRACT_UPDATE_REVIEW_STATES),
        "fresh_readiness": _bounded_contract_update_gate(fresh.get("readiness"), _CONTRACT_UPDATE_READINESS_STATES),
        **({"reason_code": reason_code} if status == "failed" else {}),
    }


def _commands_from_plan(plan: dict, issue_number: int, repo: str) -> list[dict]:
    """Build commands[] from ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 preflight.run entry."""
    try:
        from command_registry import render_command as _render_command, REGISTRY as _REGISTRY

        _entry = _REGISTRY.get("preflight.run", {})
        _argv = _render_command("preflight.run", {"issue_number": issue_number, "repo": repo})
    except Exception as exc:
        raise RuntimeError(f"BLOCKER_COMMAND_REGISTRY_UNAVAILABLE: command_registry render failed: {exc}") from exc
    return [
        {
            "kind": "run_preflight",
            "argv": _argv,
            "shell": False,
            "source": "registry",
        }
    ]


def _emit_failure_result(
    *,
    repo_root: Path,
    issue_number: int,
    repo: str,
    status: str,
    next_action: str,
    blockers: list[str],
    planner_exit_code: Optional[int] = None,
    planner_fail_closed: Optional[bool] = None,
    planner_input: Optional[dict] = None,
    raw_snapshot: Optional[dict] = None,
    planner_fail_closed_reason_codes: Optional[list[str]] = None,
    required_sections: Optional[list[str]] = None,
    required_contract_keys: Optional[list[str]] = None,
    rewrite_constraints: Optional[dict[str, Any]] = None,
    environment_failure_reason_code: Optional[str] = None,
    environment_failure_source: Optional[str] = None,
    environment_failure_operation: Optional[str] = None,
) -> tuple[dict, int]:
    """
    Build a failure/blocked/environment_failure result, write artifacts if available,
    print compact stdout, and return (result, exit_code).

    This helper ensures stdout and disk are written from the same final result dict
    (no post-write mutation). `environment_failure_reason_code`/`_source`/`_operation`
    are forwarded to `_build_result` (Issue #2257 AC10) and are only ever
    materialized in the returned result when `status == "environment_failure"`.
    """
    # Compute hashes if raw_snapshot available
    hashes: dict[str, str] = {}
    if raw_snapshot is not None:
        snapshot_text = json.dumps(raw_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
        hashes["raw_issue_snapshot_sha256"] = _sha256(snapshot_text)
    if planner_input is not None:
        planner_input_text = json.dumps(planner_input, sort_keys=True, ensure_ascii=False, allow_nan=False)
        hashes["planner_input_sha256"] = _sha256(planner_input_text)

    artifacts: dict[str, str] = {}
    if raw_snapshot is not None and planner_input is not None:
        artifact_dir = _issue_artifact_dir(repo_root, issue_number)
        artifacts = {
            "raw_issue_snapshot": str(artifact_dir / "raw_issue_snapshot.json"),
            "planner_input": str(artifact_dir / "planner_input.json"),
            "refinement_preflight_result_v1": str(artifact_dir / "refinement_preflight_result_v1.json"),
        }

    result = _build_result(
        status=status,
        issue_number=issue_number,
        repo=repo,
        planner_exit_code=planner_exit_code,
        planner_fail_closed=planner_fail_closed,
        next_action=next_action,
        must_read=[],
        do_not_read=[],
        commands=[],
        blockers=blockers,
        planner_fail_closed_reason_codes=planner_fail_closed_reason_codes or [],
        required_sections=required_sections or [],
        required_contract_keys=required_contract_keys or [],
        rewrite_constraints=rewrite_constraints,
        artifacts=artifacts,
        hashes=hashes,
        environment_failure_reason_code=environment_failure_reason_code,
        environment_failure_source=environment_failure_source,
        environment_failure_operation=environment_failure_operation,
    )

    if raw_snapshot is not None and planner_input is not None:
        try:
            _write_artifacts(repo_root, issue_number, raw_snapshot, planner_input, result)
        except Exception as exc:
            result["blockers"] = [
                *result["blockers"],
                BLOCKER_RESULT_SCHEMA_INVALID,
                f"failure_artifact_write_error:{type(exc).__name__}:{str(exc)[:500]}",
            ]
            result["status"] = "environment_failure"
            result["next_action"] = "fix_environment"
            result["artifacts"] = {}
            # Issue #2257 AC10: this failure path transitions `status` to
            # `environment_failure` after `_build_result` already ran, so
            # the schema's conditional-required projection must be
            # populated here too (or the artifact-write failure itself
            # would leave the result RESULT_SCHEMA_INVALID by omission).
            result.setdefault("reason_code", "transport_internal_error")
            result.setdefault("source", "internal")
            result.setdefault("operation", "write_artifacts")

    _, exit_code = _apply_exit_code_mapping(planner_exit_code, planner_fail_closed, result["blockers"])
    if result["status"] == "environment_failure":
        exit_code = EXIT_ENVIRONMENT_FAILURE
    print(_build_compact_stdout(result))
    return result, exit_code


# ---------------------------------------------------------------------------
# SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (#1323) -- freeform human review comment
# evidence builder (independent of the structured ANCHOR_SCOPE_REFRAME_V1
# payload consumed by _classify_anchor_scope_reframe above).
# ---------------------------------------------------------------------------

# Heavy mutation categories (#1891 AC6): mutation intents that require an
# explicit owner-sourced decision before they may proceed. Non-heavy
# categories continue with a warning instead of a hard stop.
HEAVY_MUTATION_CATEGORIES = frozenset(
    {
        "close",
        "not_planned",
        "replacement_issue_creation",
        "dependency_removal",
        "parent_child_change",
    }
)


def _is_approved_owner_reaction_not_planned_decision(
    decision: "dict | None", *, target_issue_number: "int | None"
) -> bool:
    """#2689 AC1/AC5: independent predicate -- deliberately NOT a variant of
    `_is_approved_close_not_planned_decision()` above (never calls it, never
    reads/writes `authorized_mutation_category` / `anchor_author_association`
    / `implementation_go`, the trusted-anchor gate's own fields). Reads ONLY
    an `OWNER_REACTION_DECISION_RESULT_V1`-shaped dict (the fresh
    `owner_reaction_decision.py` subprocess output -- see
    `_run_owner_reaction_decision_fresh()`) and NEVER converts a `selected`
    status into `approved_by_trusted_anchor` (that authority semantic is
    exclusive to the pre-existing trusted-anchor gate; #2689 AC5) -- this
    function only ever returns a plain bool, it never mutates or re-labels
    `decision`.

    True only when ALL of:
      - `decision["status"] == "selected"` (rejects `unresolved` / `stale` /
        `environment_error` -- #2689 AC3)
      - `decision["selected_option_metadata"]["operation"] == "close_not_planned"`
      - the selected option's `target` is EXACTLY `f"#{target_issue_number}"`
        -- a same-operation, different-target option is never treated as
        approved (#2689 AC2, matching the identity-binding contract fixed by
        `test_preview_binding_primary_identity` in
        `test_owner_reaction_decision.py`).
    """
    if target_issue_number is None or isinstance(target_issue_number, bool):
        return False
    if not isinstance(decision, dict):
        return False
    if decision.get("status") != "selected":
        return False
    metadata = decision.get("selected_option_metadata")
    if not isinstance(metadata, dict):
        return False
    if metadata.get("operation") != "close_not_planned":
        return False
    target = metadata.get("target")
    return isinstance(target, str) and target == f"#{target_issue_number}"


def _build_owner_reaction_decide_argv(
    *, repo: str, issue_number: int, owner_user_id: int, preview_binding_file: str
) -> list[str]:
    """#2689 AC4: renders the SAME `--repo` / `--issue-number` /
    `--owner-user-id` / `--preview-binding-file` argument shape as
    `command_registry.py`'s production `owner_reaction.decide` entry (never
    a divergently-shaped re-implementation). This module invokes
    `owner_reaction_decision.py` directly via `sys.executable` (the SAME
    in-process-sibling-subprocess convention `_run_repair_subprocess()`
    above already uses for `repair_issue_contract.py`), not through
    `scripts/agent-guards/skill_runtime_exec.py` -- #2689 Out of Scope
    forbids adding any new dispatch surface to that executor / its
    `command_registry.py` / `skill_runtime_command_policy.py`.
    """
    return [
        sys.executable,
        str(OWNER_REACTION_DECISION_SCRIPT),
        "--repo", repo,
        "--issue-number", str(issue_number),
        "--owner-user-id", str(owner_user_id),
        "--preview-binding-file", preview_binding_file,
    ]


def _run_owner_reaction_decision_fresh(
    *,
    repo: str,
    issue_number: int,
    owner_user_id: int,
    preview_binding_file: str,
    timeout: float = 90.0,
    subprocess_runner: "Callable[..., Any] | None" = None,
) -> "dict | None":
    """#2689 AC4: ALWAYS issues a brand-new `owner_reaction_decision.py`
    subprocess -- this function holds no module-level cache/memoization of
    its own, so calling it N times in the same process performs N real
    subprocess launches, never a cached/reused result from a prior call.
    Returns the parsed `OWNER_REACTION_DECISION_RESULT_V1` dict, or `None`
    on ANY transport-level failure (nonzero exit, non-JSON stdout, launch
    error, timeout, schema mismatch). Callers MUST treat `None` as "not
    approved" (fail-closed) -- never as an implicit approval.

    PR #2697 review fix_delta (P1): a non-zero `proc.returncode` is ALWAYS
    fail-closed to `None`, even when stdout happens to be parseable JSON
    (e.g. a subprocess that prints a partial/stale payload before exiting
    non-zero) -- transport success is never inferred from stdout shape
    alone.

    PR #2697 review fix_delta (P2): a lightweight schema-identity check --
    the parsed dict's `schema` field must equal
    `OWNER_REACTION_DECISION_RESULT_V1` (the canonical
    `owner_reaction_decision.py` output schema) -- rejects any payload that
    is valid JSON but not actually shaped like the expected producer
    output. This is a single literal-equality check, not a new generic
    schema-validator framework.
    """
    runner = subprocess_runner or subprocess.run
    argv = _build_owner_reaction_decide_argv(
        repo=repo,
        issue_number=issue_number,
        owner_user_id=owner_user_id,
        preview_binding_file=preview_binding_file,
    )
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # PR #2697 review fix_delta (P1): fail-closed on ANY non-zero exit,
    # regardless of whether stdout happens to parse as JSON.
    returncode = getattr(proc, "returncode", None)
    if returncode != 0:
        return None
    stdout = getattr(proc, "stdout", None)
    if not isinstance(stdout, str) or not stdout:
        return None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # PR #2697 review fix_delta (P2): reject any payload that does not
    # carry the canonical OWNER_REACTION_DECISION_RESULT_V1 schema
    # identity, even if it otherwise parses as a JSON object.
    if parsed.get("schema") != "OWNER_REACTION_DECISION_RESULT_V1":
        return None
    return parsed


def _classify_heavy_mutation_gate(
    *,
    mutation_category: "str | None",
    scope_delta_decision: "dict | None",
    owner_reaction_decision: "dict | None" = None,
    target_issue_number: "int | None" = None,
) -> dict:
    """#1891 AC6: fail-closed gate for heavy mutation categories.

    Heavy mutation categories (close / not planned / replacement Issue
    creation / dependency removal / parent-child relationship change) are
    blocked unless the decision is the exact OWNER-sourced
    `close_not_planned` disposition bound to the `not_planned` category.
    Non-heavy mutation categories (ordinary body improvement / additional investigation / review
    continuation) are never blocked here -- they continue with a `warn`
    status, matching the pre-existing advisory-only behavior.

    `owner_reaction_decision` / `target_issue_number` (#2689, both default
    `None`, fully backward compatible -- every pre-existing caller that
    omits them observes byte-identical behavior to before #2689): an
    INDEPENDENT, `not_planned`-only OR-branch alongside the pre-existing
    trusted-anchor `exact_close_not_planned` branch above -- see
    `_is_approved_owner_reaction_not_planned_decision()`. This never changes
    `exact_close_not_planned`'s own boolean identity, and never widens
    `HEAVY_MUTATION_CATEGORIES` or the fail-closed default for any other
    heavy mutation category (#2689 Out of Scope).
    """
    decision = scope_delta_decision or {}
    is_heavy = mutation_category in HEAVY_MUTATION_CATEGORIES
    exact_close_not_planned = (
        _is_approved_close_not_planned_decision(decision)
        and mutation_category == "not_planned"
        and decision.get("authorized_mutation_category") == mutation_category
    )
    owner_reaction_not_planned_approved = mutation_category == "not_planned" and (
        _is_approved_owner_reaction_not_planned_decision(
            owner_reaction_decision, target_issue_number=target_issue_number
        )
    )

    if exact_close_not_planned or owner_reaction_not_planned_approved:
        return {
            "mutation_category": mutation_category,
            "is_heavy_mutation": is_heavy,
            "status": "allowed",
            "fail_closed": False,
            "reason": (
                "owner_close_not_planned_decision_present"
                if exact_close_not_planned
                else "owner_reaction_not_planned_decision_present"
            ),
        }

    if is_heavy:
        return {
            "mutation_category": mutation_category,
            "is_heavy_mutation": True,
            "status": "blocked",
            "fail_closed": True,
            "reason": "heavy_mutation_requires_owner_explicit_decision",
        }

    return {
        "mutation_category": mutation_category,
        "is_heavy_mutation": False,
        "status": "warn",
        "fail_closed": False,
        "reason": "non_heavy_mutation_warning_continue",
    }


def _classify_heavy_mutation_gate_with_fresh_owner_reaction(
    *,
    mutation_category: "str | None",
    scope_delta_decision: "dict | None",
    owner_reaction_context: "dict | None",
    repo: str,
    issue_number: int,
    subprocess_runner: "Callable[..., Any] | None" = None,
) -> dict:
    """#2689 AC4: production wiring wrapper around `_classify_heavy_mutation_gate()`.

    ONLY when `mutation_category == "not_planned"` AND the caller supplies
    an `owner_reaction_context` dict (`{"owner_user_id": <positive int>,
    "preview_binding_file": <repo-relative path str>}`, mirroring
    `command_registry.py`'s `owner_reaction.decide` placeholders) does this
    function issue a FRESH `owner_reaction_decision.py` subprocess -- via
    `_run_owner_reaction_decision_fresh()` -- immediately before delegating
    to `_classify_heavy_mutation_gate()`, and that SAME-invocation result
    (never a prior/cached one -- this function holds no state of its own)
    is the ONLY owner-reaction input the gate call below observes. When
    `owner_reaction_context` is absent/malformed, or `mutation_category` is
    anything other than `"not_planned"`, no subprocess is issued at all and
    `_classify_heavy_mutation_gate()` behaves byte-identically to before
    #2689.
    """
    owner_reaction_decision_fresh: "dict | None" = None
    if mutation_category == "not_planned" and isinstance(owner_reaction_context, dict):
        owner_user_id = owner_reaction_context.get("owner_user_id")
        preview_binding_file = owner_reaction_context.get("preview_binding_file")
        if (
            isinstance(owner_user_id, int)
            and not isinstance(owner_user_id, bool)
            and owner_user_id > 0
            and isinstance(preview_binding_file, str)
            and preview_binding_file
        ):
            owner_reaction_decision_fresh = _run_owner_reaction_decision_fresh(
                repo=repo,
                issue_number=issue_number,
                owner_user_id=owner_user_id,
                preview_binding_file=preview_binding_file,
                subprocess_runner=subprocess_runner,
            )

    return _classify_heavy_mutation_gate(
        mutation_category=mutation_category,
        scope_delta_decision=scope_delta_decision,
        owner_reaction_decision=owner_reaction_decision_fresh,
        target_issue_number=issue_number,
    )


def _apply_multi_turn_candidate_route(
    scope_delta_decision: dict,
    segments_result: "dict | None",
    candidates_result: "dict | None",
    *,
    integrity_predicates: "dict | None" = None,
) -> dict:
    """#1891 AC4 / #1950 AC1: route multi-turn anchors when anchor_context.py
    finds multiple unclassified candidates spread across a genuine
    multi-turn transcript (>=2 marker-delimited segments).

    This does not fire for an ordinary single-turn review comment that
    happens to contain several bullet points -- it is scoped to the
    multi-turn/supersession scenario this Issue targets, so it never
    auto-selects a single winning candidate and never silently overrides an
    existing fail-closed decision with a weaker one.

    #1950 AC1 (chronology vs. precedence vs. authorization are separate
    axes): when the anchor comment's author is a trusted OWNER
    (``scope_delta_decision["anchor_author_association"] == "OWNER"``), a
    genuine multi-turn transcript is no longer a hard stop by itself. The
    index / source span of the *last* OWNER-speaker segment is recorded as
    ``latest_owner_turn`` chronology metadata only -- it is never promoted
    to a technical_recommendation or mutation_authorization precedence
    signal, and multi-turn ambiguity alone still does not grant
    implementation_go. Non-OWNER (or untrusted) multi-turn anchors keep the
    pre-existing hard `fail_closed` route unchanged.

    PR #1973 (OWNER REQUEST_CHANGES, P0-1): the advisory `warn` route must
    never silently overwrite an ORIGINAL `approved_by_trusted_anchor`
    decision (which would break the heavy-mutation gate's OWNER-explicit
    check downstream) or an ORIGINAL `fail_closed` decision whose reason is
    something other than the ordinary "no structured payload" case (e.g.
    `schema_invalid`, `wrong_repo`, `wrong_issue_number`,
    `untrusted_author_association`) -- those reflect a distinct integrity
    problem that a multi-turn transcript does not resolve. The advisory
    downgrade is applied ONLY when the original decision is
    `fail_closed` / `no_anchor_scope_reframe_v1_payload` AND retrieval
    integrity (`integrity_predicates`: fetch complete, hash verified, source
    ranges covered) is fully confirmed.
    """
    if not isinstance(segments_result, dict) or not isinstance(candidates_result, dict):
        return scope_delta_decision

    marked_segments = [seg for seg in segments_result.get("segments", []) if seg.get("marker")]
    candidates = candidates_result.get("candidates", [])

    if len(marked_segments) >= 2 and len(candidates) >= 2:
        is_trusted_owner = scope_delta_decision.get("anchor_author_association") == "OWNER"
        owner_segments = [
            seg for seg in marked_segments if seg.get("speaker") == anchor_context.SPEAKER_OWNER
        ]

        if is_trusted_owner and owner_segments:
            last_owner_segment = owner_segments[-1]
            latest_owner_turn = {
                "segment_index": last_owner_segment.get("index"),
                "source_range": {
                    "start_line": last_owner_segment.get("start_line"),
                    "end_line": last_owner_segment.get("end_line"),
                },
                # chronology metadata only; not technical_recommendation or
                # mutation_authorization precedence (#1950 AC1).
                "note": (
                    "chronology metadata only -- not technical_recommendation "
                    "or mutation_authorization precedence"
                ),
            }

            original_status = scope_delta_decision.get("status")
            original_reason = scope_delta_decision.get("reason")

            if original_status == "approved_by_trusted_anchor":
                # Never overwrite a valid owner approval with `warn` -- the
                # heavy-mutation gate depends on this status surviving
                # unchanged.
                updated = dict(scope_delta_decision)
                updated["latest_owner_turn"] = latest_owner_turn
                return updated

            predicates = integrity_predicates or {}
            integrity_confirmed = (
                predicates.get("source_fetch_complete") is True
                and predicates.get("source_hash_verified") is True
                and predicates.get("source_ranges_covered") is True
            )

            # #2156 AC5/AC6: `no_anchor_scope_reframe_v1_payload` (genuine
            # absence) is produced by `_classify_anchor_scope_reframe()`
            # with `status: not_applicable` now (AC2), but legacy/fixture
            # callers may still pass a `status: fail_closed` decision
            # carrying the same reason -- both upstream statuses must be
            # recognized here so the advisory-upgrade / blocking-preserved
            # invariants do not silently stop firing.
            if original_reason == "no_anchor_scope_reframe_v1_payload" and original_status in (
                "fail_closed",
                "not_applicable",
            ):
                if integrity_confirmed:
                    updated = dict(scope_delta_decision)
                    updated["anchor_context_candidate_count"] = len(candidates)
                    updated["anchor_context_marked_segment_count"] = len(marked_segments)
                    updated["implementation_go"] = False
                    updated["status"] = "warn"
                    updated["reason"] = "multi_turn_anchor_context_trusted_owner_advisory"
                    updated["latest_owner_turn"] = latest_owner_turn
                    return updated

                if original_status == "not_applicable":
                    # #2156 AC6: the upstream genuine-absence status is
                    # non-blocking (`not_applicable`), but multi-turn
                    # retrieval integrity is unconfirmed here -- this must
                    # not silently remain non-blocking. Force back to a
                    # blocking `fail_closed` state rather than letting the
                    # upstream not_applicable downgrade survive unchanged.
                    #
                    # PR #2171 fix_delta (P0-1, OWNER adversarial review):
                    # the `reason` must change to a dedicated value here --
                    # leaving it as `no_anchor_scope_reframe_v1_payload`
                    # (the upstream not_applicable reason) meant the
                    # caller's canonical blocker conversion (which does an
                    # exact match on
                    # `multi_turn_anchor_context_requires_human_judgment`)
                    # never recognized this route, so it silently never
                    # reached `blockers` / the final exit code.
                    updated = dict(scope_delta_decision)
                    updated["status"] = "fail_closed"
                    updated["reason"] = (
                        "multi_turn_anchor_context_retrieval_integrity_unconfirmed"
                    )
                    updated["implementation_go"] = False
                    updated["latest_owner_turn"] = latest_owner_turn
                    return updated

                # original_status == "fail_closed" with unconfirmed
                # integrity: pre-existing behavior, unchanged -- legacy
                # callers/fixtures already representing a blocking decision
                # are returned untouched (identity).
                return scope_delta_decision

            # Any other original status/reason (schema_invalid, wrong_repo,
            # wrong_issue_number, untrusted_author_association) is returned
            # unchanged -- the multi-turn advisory route never masks a
            # distinct integrity problem.
            return scope_delta_decision

        updated = dict(scope_delta_decision)
        updated["anchor_context_candidate_count"] = len(candidates)
        updated["anchor_context_marked_segment_count"] = len(marked_segments)
        updated["implementation_go"] = False
        updated["status"] = "fail_closed"
        updated["reason"] = "multi_turn_anchor_context_requires_human_judgment"
        return updated

    return scope_delta_decision


def _structured_anchor_payload_present_but_invalid(scope_delta_decision: "dict | None") -> bool:
    """#2053 AC2: distinguish "no ANCHOR_SCOPE_REFRAME_V1 payload at all"
    (legitimate freeform lane, e.g. Issue #1270) from "a structured payload
    WAS present but is invalid/stale/wrong-target". Only the latter must
    forbid downgrading to freeform authority evidence built from the same
    comment body -- accepting the same untrusted-shape text as freeform
    directive would let an attacker/mistake dodge structured schema
    validation simply by making the payload fail it.

    `_classify_anchor_scope_reframe()` reason strings for a payload that WAS
    parsed but rejected are: "schema_invalid: ...", "wrong_repo: ...",
    "wrong_issue_number: ...", "stale: ..." (#2053 P2 fix-delta iteration 3
    -- the comment's own `created_at`/`updated_at` no longer match, i.e. it
    was edited after the original approval). "no_anchor_scope_reframe_v1_payload"
    (no payload found) and "untrusted_author_association: ..." (author
    trust, independent of payload presence) are NOT included -- an
    untrusted author's freeform text is already fail-closed downstream by
    classify_scope_delta_authority()'s own author-association check.
    """
    if not isinstance(scope_delta_decision, dict):
        return False
    if scope_delta_decision.get("status") != "fail_closed":
        return False
    reason = scope_delta_decision.get("reason") or ""
    return reason.startswith(("schema_invalid:", "wrong_repo:", "wrong_issue_number:", "stale:"))


def _bind_trusted_scope_patch_plan_to_source(
    plan: dict, known_context: dict, *, anchor_body: str, anchor_url: str,
    repo: str, issue_number: int, issue_body_sha256: str,
) -> None:
    """Replace the planner's evidence-only patch plan with a body-bound one.

    The planner subprocess only receives schema-bound extracted evidence; it
    cannot receive raw OWNER text. Before any consumer sees its sidecar, the
    already-fetched trusted comment is bound here by URL and body hash and
    classified again. Raw text never enters the plan or artifact.
    """
    sidecar = plan.get("scope_signal_guard_decision_v2")
    authority = sidecar.get("scope_delta_authority") if isinstance(sidecar, dict) else None
    evidence = known_context.get("scope_delta_authority_evidence")
    if (not isinstance(authority, dict)
            or not isinstance(authority.get("contract_patch_plan"), dict)
            or not isinstance(evidence, list) or len(evidence) != 1
            or not isinstance(evidence[0], dict)
            or evidence[0].get("source_kind") != "issue_comment"
            or evidence[0].get("comment_url") != anchor_url):
        return
    from scope_signal_delta import classify_scope_delta_authority

    sidecar["scope_delta_authority"] = classify_scope_delta_authority(
        evidence, source_body=anchor_body, target_issue_number=issue_number,
        expected_repo=repo, base_issue_body_sha256=issue_body_sha256,
        investigation_derived_path_literals=known_context.get("investigation_derived_path_literals"),
    )


def _build_scope_delta_authority_evidence(
    *,
    comment_payload: dict,
    comment_body: str,
    repo: str,
    issue_number: int,
    anchor_url: str,
    captured_at: str,
    human_context_comment_urls: Any = None,
    agent_report_comment_urls: Any = None,
    segments_result: "dict | None" = None,
    candidates_result: "dict | None" = None,
):
    """#1323: build SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 from an anchor comment.

    Unlike _classify_anchor_scope_reframe (which requires a structured
    ANCHOR_SCOPE_REFRAME_V1 fenced yaml payload), this works on freeform
    human review comments (e.g. Issue #1270's Revised Acceptance Criteria
    comment) so explicit human-review directives are not silently dropped
    just because they are not machine-formatted.

    Returns None (fail-closed) when the anchor URL does not structurally
    resolve to an issue-comment on `issue_number` in `repo` (AC16). Never
    forwards the raw comment body -- only sha256 + extracted markers /
    directives / boundary flags (AC14).

    #1891 AC4 (iteration 2, PR #1923 OWNER REQUEST_CHANGES): `segments_result`
    / `candidates_result` are the anchor_context.py pure-analyzer outputs for
    this same `comment_body`. They are accepted here (instead of only being
    threaded through a disconnected downstream call) so the multi-turn
    candidate route is wired at the same call site that builds the scope
    delta authority evidence, per the Issue's In Scope wiring requirement.
    They are intentionally NOT added to the returned
    SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 dict (its schema is `additionalProperties:
    false`); the caller applies `_apply_multi_turn_candidate_route()` to
    `scope_delta_decision` immediately after this call using the same
    `segments_result` / `candidates_result` values.
    """
    try:
        from scope_signal_delta import (
            classify_directive_confidence,
            detect_boundary_flags,
            extract_directive_items,
            extract_directive_markers,
            parse_issue_comment_url,
        )
    except ImportError:
        return None

    parsed = parse_issue_comment_url(anchor_url)
    if parsed is None:
        return None
    if f"{parsed['owner']}/{parsed['repo']}".lower() != repo.lower():
        return None
    if parsed["issue_number"] != issue_number:
        return None

    author_association = comment_payload.get("author_association")
    user = comment_payload.get("user")
    if isinstance(user, dict):
        author_login = user.get("login")
        author_type = user.get("type") or "unknown"
    else:
        author_login = comment_payload.get("author_login")
        author_type = comment_payload.get("author_type") or "unknown"

    markers = extract_directive_markers(comment_body)
    directives = extract_directive_items(comment_body)
    boundary_flags_map = detect_boundary_flags(comment_body)
    boundary_flag_names = [name for name, value in boundary_flags_map.items() if value]
    # #2086 AC1/AC3/AC6: source_kind must be resolved before confidence is
    # classified so that the operator-selected human-context lane (the only
    # lane where `source_kind == "issue_comment"`, see
    # `_resolve_scope_delta_source_kind()`) can relax the "known marker
    # heading required" rule for freeform directives. `with_agent_report` /
    # unlabeled anchors always resolve to `generated_by_agent` here and never
    # reach this relaxation.
    source_kind = _resolve_scope_delta_source_kind(
        anchor_url,
        human_context_comment_urls=human_context_comment_urls,
        agent_report_comment_urls=agent_report_comment_urls,
    )
    confidence = classify_directive_confidence(
        comment_body,
        markers,
        operator_asserted_human_context=(source_kind == "issue_comment"),
    )

    issue_url = f"https://github.com/{repo}/issues/{issue_number}"

    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": source_kind,
        "source_ref": anchor_url,
        "source_issue_number": issue_number,
        "comment_id": comment_payload.get("id"),
        "comment_url": anchor_url,
        "issue_url": issue_url,
        "body_sha256": _sha256(comment_body),
        "author_login": author_login,
        "author_type": author_type,
        "author_association": author_association,
        "captured_at": captured_at,
        "directive_markers": markers,
        "extracted_directives": directives,
        "ambiguity_flags": [] if confidence != "ambiguous" else ["structured_list_missing"],
        "boundary_flags": boundary_flag_names,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# #2048 Scope Delta: consumer-boundary scope-reframe disposition helpers
# ---------------------------------------------------------------------------


def _classify_scope_delta_decision(
    *,
    known_context: Optional[dict[str, Any]],
    anchor_url: str,
    anchor_body: str,
) -> "tuple[str, list[str] | None]":
    """Classify `known_context["scope_delta_decision"]` into a tri-state.

    Returns ``(kind, deltas)`` where ``kind`` is one of:

    - ``"absent"``: no scope-reframe decision was supplied at all (this is
      an ordinary, non-scope-reframe patch-plan consumer call --
      `deltas` is `None`).
    - ``"invalid"``: a decision IS present but fails validation or binding
      (malformed `allowed_path_deltas` type/blank entries, or an
      anchor URL/hash mismatch against THIS consumer call's anchor/body).
      PR #2057 OWNER review P0-1: this is a DISTINCT outcome from
      ``"absent"`` -- the pre-fix implementation collapsed both into the
      same ``None`` return, so an invalid decision was silently treated as
      an ordinary no-op (`proven_no_change`-equivalent) instead of
      `invalid`.
    - ``"valid"``: `deltas` is the validated non-empty ``list[str]``, bound
      to this anchor URL and anchor body hash.
    """
    if not isinstance(known_context, dict):
        return "absent", None
    decision = known_context.get("scope_delta_decision")
    if not isinstance(decision, dict):
        return "absent", None
    if decision.get("status") != "approved_by_trusted_anchor":
        return "absent", None
    deltas = decision.get("allowed_path_deltas")
    if deltas is None:
        return "absent", None
    if not isinstance(deltas, list):
        return "invalid", None
    if not deltas:
        # An explicit empty list is not a scope-reframe signal at all
        # (consistent with `classify_scope_reframe_disposition()`'s own
        # `is_approved_scope_reframe = ... and bool(normalized_deltas)`
        # gate) -- "absent", not "invalid". Only a non-list type or a
        # blank/whitespace entry WITHIN a non-empty list is `invalid`.
        return "absent", None
    if not all(isinstance(item, str) and item.strip() for item in deltas):
        return "invalid", None
    if decision.get("anchor_comment_url") != anchor_url:
        return "invalid", None
    if decision.get("anchor_comment_hash") != _sha256(anchor_body):
        return "invalid", None
    return "valid", list(deltas)


def _extract_validated_scope_delta_deltas(
    *,
    known_context: Optional[dict[str, Any]],
    anchor_url: str,
    anchor_body: str,
) -> "list[str] | None":
    """Back-compat wrapper: returns the validated deltas, or `None` for
    both `"absent"` and `"invalid"` (callers that only need existence, not
    the distinction, e.g. the run_preflight() empty-patch-plan-synthesis
    gate). See `_classify_scope_delta_decision()` for the tri-state form
    that `consume_trusted_anchor_contract_patch_plan()` uses to distinguish
    `invalid` from an ordinary absent decision.
    """
    _, deltas = _classify_scope_delta_decision(
        known_context=known_context, anchor_url=anchor_url, anchor_body=anchor_body
    )
    return deltas


def _normalize_scope_reframe_delta_literal(raw: str) -> str:
    """Strip bullet/backtick decoration from an `allowed_path_deltas` entry.

    `ANCHOR_SCOPE_REFRAME_V1.allowed_path_deltas` entries are free-form
    strings (see `anchor_scope_reframe_v1.schema.json`); in practice trusted
    anchors write them as Markdown bullet items (e.g.
    ``"- `docs/x.md`"``). This normalizes to a bare path/pattern comparable
    against `baseline_vc_preflight.extract_allowed_paths()` output.
    """
    text = raw.strip()
    for prefix in ("- ", "* ", "+ "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.strip("`").strip()


def _check_scope_reframe_deltas_reflected(
    *,
    current_body: str,
    allowed_path_deltas: list[str],
) -> str:
    """#2048 / PR #2057 OWNER review P1-5: tri-state Allowed Paths
    postcondition check.

    Returns one of:
      - ``"present"``: every `allowed_path_deltas` entry is present in the
        canonical `## Allowed Paths` section's EXACT normalized set (real
        `proven_no_change` disposition).
      - ``"absent"``: the canonical parser loaded successfully but at least
        one delta entry is not in the exact set (real
        `full_rewrite_required` candidate, pending the empty-operations
        classifier).
      - ``"invalid_or_unavailable"``: the canonical parser could not be
        loaded/executed, OR `allowed_path_deltas` is empty. Never silently
        treated as "absent" -- the caller's classifier maps this to
        `invalid`, since "the rewrite may already be reflected but we could
        not check" must never authorize a full-body-rewrite handoff, nor a
        false proven-satisfied no-op.

    The previous implementation additionally accepted
    ``normalized_delta in current_body`` (a whole-body substring fallback)
    whenever the delta was not found in the canonical Allowed Paths section.
    That fallback is REMOVED: it could match a delta literal that only
    appears in `## Outcome`, `## Notes`, a fenced code block, prose, or as a
    substring of an unrelated longer path, misclassifying `full_rewrite_
    required` as `proven_no_change`. Only the canonical section's exact
    normalized set is used.
    """
    if not allowed_path_deltas:
        return "invalid_or_unavailable"
    try:
        import importlib.util

        baseline_path = (
            _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
        )
        baseline_spec = importlib.util.spec_from_file_location(
            "scope_reframe_allowed_paths_baseline", baseline_path
        )
        if baseline_spec is None or baseline_spec.loader is None:
            return "invalid_or_unavailable"
        baseline_module = importlib.util.module_from_spec(baseline_spec)
        baseline_spec.loader.exec_module(baseline_module)
        existing = set(baseline_module.extract_allowed_paths(current_body) or [])
    except Exception:
        return "invalid_or_unavailable"
    normalized_existing = {_normalize_scope_reframe_delta_literal(path) for path in existing}
    for delta in allowed_path_deltas:
        normalized_delta = _normalize_scope_reframe_delta_literal(delta)
        if not normalized_delta or normalized_delta not in normalized_existing:
            return "absent"
    return "present"


def _human_review_directive_route_environment_failure(reason_code: str) -> dict[str, Any]:
    """PR #2623 review fix (P1 finding 3): a distinguishable, fail-closed
    outcome for integrity/environment failures inside
    `_decide_human_review_directive_editor_route()` (broken import,
    classifier exception, fresh-readback transport failure).

    Reuses the EXISTING `status: "invalid"` / `disposition: {...}` vocabulary
    `consume_trusted_anchor_contract_patch_plan()` already emits for
    `_decision_kind == "invalid"` (binding-mismatched scope-delta decision,
    see immediately above this function's caller) -- no new schema/key-set
    is introduced. `_bounded_contract_update_handoff()` already projects
    this exact shape into `status: "failed"`, `disposition: "invalid"`,
    `writes: 0`, `reason_code: <reason_code>`.

    This is intentionally distinct from returning `None`: `None` means
    "this route has no opinion, fall through to the existing fail-closed /
    no_change behavior" (a normal, non-failure outcome for evidence this
    route simply doesn't apply to). An environment/integrity failure must
    never be silently absorbed into that same ordinary fallback -- it would
    surface as an innocuous `no_change`/`proven_no_change` result even
    though the routing decision itself could not be trusted.
    """
    return {
        "status": "invalid",
        "disposition": {
            "schema_version": "scope_reframe_decision/v1",
            "disposition": "invalid",
            "reason_code": reason_code,
        },
        "writes": 0,
        "iterations": 0,
    }


def _decide_human_review_directive_editor_route(
    *,
    known_context: Optional[dict[str, Any]],
    anchor_url: str,
    anchor_body: str,
    issue_number: int,
    repo: str,
    issue_body_sha256: str,
    fetch_current: Optional[Callable[[], tuple[dict, dict]]] = None,
) -> Optional[dict[str, Any]]:
    """#2620: assemble the existing-predicate facts
    `decide_rewrite_route.decide_human_review_directive_editor_route()`
    (the routing SSOT) needs, and project its result into the same shape
    `consume_trusted_anchor_contract_patch_plan()` returns for its other
    dispositions.

    This function performs NO eligibility classification of its own --
    every fact it supplies is either read verbatim from
    `known_context["scope_delta_authority_evidence"]` (the EXISTING
    `_build_scope_delta_authority_evidence()` producer output already
    computed earlier in this same preflight run) or freshly re-derived by
    calling the EXISTING `scope_signal_delta.classify_scope_delta_authority()`
    classifier again against that same evidence (matching the "fresh
    rerun" pattern already used elsewhere in this file, e.g.
    `consume_authority_transport()`'s own re-classification). The actual
    routing decision is made entirely by the SSOT function in
    `decide_rewrite_route.py`.

    Returns `None` when there is no freeform
    `SCOPE_DELTA_AUTHORITY_EVIDENCE_V1` to classify at all, when the SSOT
    function determines this transaction is not eligible, or when a fresh
    `fetch_current()` readback (see below) shows the anchor has changed --
    callers MUST fall through to the existing behavior (no_change /
    fail-closed) in all three cases. Returns a distinguishable
    `_human_review_directive_route_environment_failure()` dict (never
    `None`) for an integrity/environment failure (broken import, classifier
    exception, or an unreachable/failing fresh readback) -- see that
    helper's docstring for why these must never collapse into the same
    `None` as ordinary ineligibility.

    PR #2623 review fix (P1 finding 2): once the SSOT determines this
    transaction IS eligible, this function performs one more, genuinely
    fresh `fetch_current()` re-read of the anchor comment -- never trusting
    the (possibly stale, already-in-hand) `anchor_body` / evidence alone --
    and only authorizes the handoff when the freshly re-read anchor's body
    and identity still match. This closes the same TOCTOU window
    `run_trusted_anchor_iteration_zero()` already closes for the
    STRUCTURED scope-reframe lane, for this freeform lane too.
    """
    if not isinstance(known_context, dict):
        return None
    evidence_list = known_context.get("scope_delta_authority_evidence")
    if not isinstance(evidence_list, list) or not evidence_list:
        return None
    evidence_primary = evidence_list[0]
    if not isinstance(evidence_primary, dict):
        return None

    try:
        from decide_rewrite_route import (
            ROUTE_ISSUE_EDITOR_REQUIRED,
            HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1,
            decide_human_review_directive_editor_route,
        )
        from scope_signal_delta import classify_scope_delta_authority
    except ImportError:
        return _human_review_directive_route_environment_failure(
            "human_review_directive_route_import_failed"
        )

    # Fresh, independent re-classification against the SAME evidence --
    # never trust the caller-supplied patch plan's mere shape.
    try:
        authority = classify_scope_delta_authority(
            evidence_list,
            target_issue_number=issue_number,
            expected_repo=repo,
            base_issue_body_sha256=issue_body_sha256,
            # PR #2623 review fix (P1 finding 1): forward the SAME
            # producer-validated `investigation_derived_path_literals`
            # transport `_ensure_scope_signal_delta_input()` /
            # `_validate_investigation_evidence_transport()` already placed
            # on `known_context` earlier in this preflight run (see the
            # `investigation_evidence_transport_path is not None` block
            # near `main()`'s planner invocation). This function performs
            # no new validation/normalization of its own -- it forwards the
            # already-validated value verbatim, the same way
            # `classify_scope_delta_authority()`'s other production call
            # sites in this file do.
            investigation_derived_path_literals=known_context.get(
                "investigation_derived_path_literals"
            ),
        )
    except Exception:
        return _human_review_directive_route_environment_failure(
            "human_review_directive_route_classifier_error"
        )
    if not isinstance(authority, dict):
        return _human_review_directive_route_environment_failure(
            "human_review_directive_route_classifier_error"
        )

    with_human_context = (
        _resolve_scope_delta_source_kind(
            anchor_url,
            human_context_comment_urls=known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD),
            agent_report_comment_urls=known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD),
        )
        == "issue_comment"
    )
    anchor_binding_ok = (
        evidence_primary.get("comment_url") == anchor_url
        and evidence_primary.get("body_sha256") == _sha256(anchor_body)
    )
    same_target_ok = evidence_primary.get("source_issue_number") == issue_number

    fresh_patch_plan = authority.get("contract_patch_plan")
    fresh_operations = fresh_patch_plan.get("operations") if isinstance(fresh_patch_plan, dict) else None
    operations_empty = isinstance(fresh_operations, list) and not fresh_operations

    route_state = HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1(
        authority_category=authority.get("authority_category"),
        directive_confidence=(authority.get("directive") or {}).get("confidence"),
        route_action=(authority.get("route") or {}).get("action"),
        with_human_context=with_human_context,
        anchor_binding_ok=bool(anchor_binding_ok),
        same_target_ok=bool(same_target_ok),
        operations_empty=bool(operations_empty),
        # The caller only reaches this helper when `_decision_kind ==
        # "absent"` -- no STRUCTURED ANCHOR_SCOPE_REFRAME_V1 decision
        # governs this transaction, so priority 2 does not apply here.
        is_structured_scope_reframe=False,
        reviewer_feedback_url=anchor_url,
    )
    route_result = decide_human_review_directive_editor_route(route_state)
    if route_result.route != ROUTE_ISSUE_EDITOR_REQUIRED:
        return None

    # PR #2623 review fix (P1 finding 2): the SSOT's eligibility decision
    # above was computed from `anchor_body` / evidence the CALLER already
    # had in hand at invocation time -- it is not itself a fresh read. A
    # concurrent anchor edit or trust revocation between that snapshot being
    # captured and this decision must never be authorized on stale
    # evidence. `fetch_current` is REQUIRED to authorize a handoff; when the
    # caller did not inject it, fail closed to `None` (existing fallback
    # applies -- it performs its own fresh readback).
    if fetch_current is None:
        return None
    try:
        fresh_issue, fresh_anchor = fetch_current()
    except Exception:
        return _human_review_directive_route_environment_failure(
            "human_review_directive_route_fresh_readback_failed"
        )
    if not isinstance(fresh_issue, dict) or not isinstance(fresh_anchor, dict):
        return _human_review_directive_route_environment_failure(
            "human_review_directive_route_fresh_readback_failed"
        )
    fresh_anchor_body = fresh_anchor.get("body", "")
    fresh_binding_ok = (
        fresh_anchor.get("html_url") == anchor_url and _sha256(fresh_anchor_body) == _sha256(anchor_body)
    )
    if not fresh_binding_ok:
        # The anchor changed (body/identity/binding) since this evidence
        # was captured -- never authorize a handoff on stale evidence. Fall
        # through to the existing (also fresh-readback-safe) fallback
        # instead of inventing a new outcome for this detected-but-benign
        # state change.
        return None

    return {
        "status": "handoff_required",
        "writes": 0,
        "iterations": 0,
        "rewrite_route": route_result.to_dict(),
        "reviewer_feedback_url": route_result.reviewer_feedback_url,
    }


def consume_trusted_anchor_contract_patch_plan(
    *,
    repo: str,
    issue_number: int,
    issue: dict,
    anchor_url: str,
    anchor_payload: dict,
    anchor_body: str,
    contract_patch_plan: Optional[dict],
    callbacks: Optional[dict[str, Any]] = None,
    known_context: Optional[dict[str, Any]] = None,
    patch_plan_producer_available: bool = True,
) -> dict:
    """Connect an approved patch plan to the controlled transaction lane.

    The planner remains read-only. This explicit consumer accepts an existing
    ``CONTRACT_PATCH_PLAN_V1`` or the narrowly represented missing-plan state
    from the opt-in preflight execution path. Its default callbacks use the
    existing readiness checker and ``edit_issue_txn.py``; tests can inject
    fixture callbacks without a GitHub mutation.
    """
    from scope_signal_delta import is_safe_contract_patch_append, run_trusted_anchor_iteration_zero

    callbacks = callbacks or {}
    temporary_paths: list[Path] = []

    # #2048 Scope Delta: `contract_patch_plan["operations"]` must be a real
    # list. A malformed type (not a list) is never coerced to `[]` -- that
    # would silently treat an invalid/untyped payload as an ordinary no-op
    # replay. Fail closed instead: no rewrite_route classification, no
    # mutation attempt.
    # #2724: a missing/non-dict planner plan is represented as ``None`` at
    # this private consumer boundary, never materialized as ``operations: []``.
    # The latter is a genuine plan representation whose ordinary no-op path
    # must remain unavailable to missing-plan input.
    _missing_contract_patch_plan = not isinstance(contract_patch_plan, dict)
    _raw_operations = [] if _missing_contract_patch_plan else contract_patch_plan.get("operations", [])
    if not isinstance(_raw_operations, list):
        return {
            "status": "blocked",
            "failure": "contract_patch_plan_operations_not_list",
            "writes": 0,
            "iterations": 0,
        }
    # Independently validate every append before readiness or GitHub
    # callbacks. Preserve the existing section-bound replace/upsert/remove
    # operations: they have their own builder validation and must not be
    # conflated with the unsafe raw freeform append fallback.
    if any(
        isinstance(op, dict) and op.get("op", op.get("kind")) == "append"
        and not is_safe_contract_patch_append(op, anchor_body=anchor_body)
        for op in _raw_operations
    ):
        return {
            "status": "blocked",
            "failure": "unsafe_unstructured_patch_operation",
            "writes": 0,
            "iterations": 0,
        }

    # #2048 Scope Delta: join the trusted structured scope-reframe
    # classification (`known_context["scope_delta_decision"]`, produced by
    # `_classify_anchor_scope_reframe()` further up the call chain) to this
    # consumer boundary. This is the ONLY production call site that invokes
    # `run_trusted_anchor_iteration_zero()`'s `allowed_path_deltas` /
    # `scope_delta_status` kwargs -- without this join, an approved scope
    # reframe with empty `operations[]` could never reach the classifier
    # from `contract_update.run.with_*` (the #2048 PR review blocker across
    # iteration 1/2/3). PR #2057 OWNER review P0-1: `_classify_scope_delta_
    # decision()` returns a tri-state (absent/invalid/valid) so a decision
    # that IS present but binding-mismatched is `invalid`, never silently
    # treated the same as "no scope reframe at all".
    _decision_kind, _validated_allowed_path_deltas = _classify_scope_delta_decision(
        known_context=known_context,
        anchor_url=anchor_url,
        anchor_body=anchor_body,
    )
    if _decision_kind == "invalid":
        return {
            "status": "invalid",
            "disposition": {
                "schema_version": "scope_reframe_decision/v1",
                "disposition": "invalid",
                "reason_code": "invalid_scope_delta_decision_binding",
            },
            "writes": 0,
            "iterations": 0,
        }

    # PR #2623 review fix (P1 finding 2): `fetch_current()` is hoisted
    # above the #2620 freeform editor-route branch below so that branch can
    # perform its own fresh, independent GitHub re-read immediately before
    # authorizing an issue-editor handoff -- closing the same TOCTOU window
    # `run_trusted_anchor_iteration_zero()` already closes for the
    # STRUCTURED scope-reframe lane (see the comment further down). This is
    # the SAME primitive (and the SAME `callbacks["fetch_current"]`
    # injection point) `run_trusted_anchor_iteration_zero()` is given below;
    # no new GitHub access layer is introduced.
    def fetch_current() -> tuple[dict, dict]:
        injected = callbacks.get("fetch_current")
        if injected is not None:
            return injected()
        current_issue, issue_error = _fetch_issue(repo, issue_number)
        if current_issue is None:
            raise RuntimeError(f"issue_readback_failed:{issue_error}")
        comment_id = anchor_payload.get("id")
        current_anchor, anchor_error = _fetch_single_comment(repo, int(comment_id))
        if current_anchor is None:
            raise RuntimeError(f"anchor_readback_failed:{anchor_error}")
        current_anchor = dict(current_anchor)
        current_anchor["html_url"] = anchor_url
        current_anchor["source_body_sha256"] = f"sha256:{_sha256(current_anchor.get('body', ''))}"
        return current_issue, current_anchor

    def _human_review_directive_editor_route() -> Optional[dict[str, Any]]:
        return _decide_human_review_directive_editor_route(
            known_context=known_context,
            anchor_url=anchor_url,
            anchor_body=anchor_body,
            issue_number=issue_number,
            repo=repo,
            issue_body_sha256=_sha256(issue.get("body", "")),
            fetch_current=fetch_current,
        )

    # #2724: route the planner-missing state through the existing canonical
    # consumer and SSOT only when no structured scope reframe governs it.
    # Ineligible freeform input remains fail-closed here rather than falling
    # through to the genuine empty-operations no-change behavior below.
    if _missing_contract_patch_plan:
        if _decision_kind == "absent":
            _editor_route_result = _human_review_directive_editor_route()
            if _editor_route_result is not None:
                return _editor_route_result
        return {
            "status": "blocked",
            "failure": "contract_patch_plan_missing",
            "writes": 0,
            "iterations": 0,
        }

    # Reject an unsafe mixed trusted comment even if a stale producer supplied
    # a *partial but individually valid* append. The empty-plan editor route
    # below retains its existing priority and eligibility (#2785).
    if _decision_kind == "absent" and _raw_operations:
        from scope_signal_delta import derive_contract_patch_operations

        evidence_list = known_context.get("scope_delta_authority_evidence") if isinstance(known_context, dict) else None
        if isinstance(evidence_list, list) and any(
            isinstance(item, dict)
            and item.get("directive_markers") and item.get("extracted_directives")
            and item.get("body_sha256") == _sha256(anchor_body)
            and not derive_contract_patch_operations([item], source_body=anchor_body)
            for item in evidence_list
        ):
            return {
                "status": "blocked", "failure": "unsafe_unstructured_patch_operation",
                "writes": 0, "iterations": 0,
            }

    # #2620: an explicit trusted human_review_directive whose derived
    # operations[] is empty (no safe section-bound patch representation)
    # and which is NOT governed by a STRUCTURED ANCHOR_SCOPE_REFRAME_V1
    # scope reframe (`_decision_kind == "absent"` -- a "valid" decision is
    # priority 2, owned by decide_scope_reframe_contract_route() further
    # below via run_trusted_anchor_iteration_zero(), and is left
    # untouched). Without this branch, `run_trusted_anchor_iteration_
    # zero()`'s own `classify_scope_reframe_disposition()` call is only
    # reachable when `allowed_path_deltas` is non-empty (the STRUCTURED
    # lane), so this freeform case would otherwise silently fall through
    # to an ordinary "no_change" no-op instead of hand off to issue-editor
    # -- the exact #2620 control-plane gap.
    if _decision_kind == "absent" and not _raw_operations:
        _editor_route_result = _human_review_directive_editor_route()
        if _editor_route_result is not None:
            return _editor_route_result
        # Keep #2620/#2785 editor eligibility untouched: the SSOT got first
        # refusal. Only then distinguish an unsafe marker-backed freeform
        # directive whose producer emitted zero safe operations from an
        # ordinary, genuinely empty/no-change plan. No mutation is attempted.
        from scope_signal_delta import derive_contract_patch_operations

        evidence_list = known_context.get("scope_delta_authority_evidence") if isinstance(known_context, dict) else None
        if isinstance(evidence_list, list) and any(
            isinstance(item, dict)
            and item.get("directive_markers")
            and item.get("extracted_directives")
            and not derive_contract_patch_operations([item], source_body=anchor_body)
            for item in evidence_list
        ):
            return {
                "status": "blocked",
                "failure": "unsafe_unstructured_patch_operation",
                "writes": 0,
                "iterations": 0,
            }

    # PR #2057 OWNER review P1-4/P1-5: the Allowed-Paths-reflected tri-state
    # is intentionally NOT computed here against `issue["body"]` (the
    # pre-fetch snapshot passed into this function). It is computed by
    # `run_trusted_anchor_iteration_zero()` itself, against a FRESH
    # `fetch_current()` re-read, immediately before it decides between
    # `proven_no_change` and `full_rewrite_required` -- closing the TOCTOU
    # window where the Issue body could have been edited (by a concurrent
    # issue-editor write reflecting the very delta this call is
    # classifying) between this consumer being invoked and the disposition
    # being decided.
    def _reflected_checker(fresh_body: str) -> str:
        return _check_scope_reframe_deltas_reflected(
            current_body=fresh_body,
            allowed_path_deltas=_validated_allowed_path_deltas or [],
        )

    def candidate_readiness(candidate_body: str) -> dict:
        injected = callbacks.get("candidate_readiness")
        if injected is not None:
            return injected(candidate_body)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(candidate_body)
            candidate_path = Path(handle.name)
        temporary_paths.append(candidate_path)
        readiness_script = (
            _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
        )
        completed = subprocess.run(
            [sys.executable, str(readiness_script), "--body-file", str(candidate_path), "--mode", "static"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        try:
            readiness = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {
                "status": "input_or_runtime_error",
                "body_sha256": f"sha256:{_sha256(candidate_body)}",
                "source_checks": [],
                "errors": [],
                "readiness_result_ref": "transaction-local",
            }
        if not isinstance(readiness, dict):
            return {
                "status": "input_or_runtime_error",
                "body_sha256": f"sha256:{_sha256(candidate_body)}",
                "source_checks": [],
                "errors": [],
                "readiness_result_ref": "transaction-local",
            }
        readiness["readiness_result_ref"] = "transaction-local"
        return readiness

    def apply_transaction(current_issue: dict, candidate_body: str, readiness: dict) -> dict:
        injected = callbacks.get("apply_transaction")
        if injected is not None:
            return injected(current_issue, candidate_body, readiness)
        repo_root = _find_repo_root()
        candidate_path = repo_root / "tmp" / f"issue_{issue_number}_trusted_anchor_candidate.md"
        input_path = repo_root / "tmp" / f"issue_{issue_number}_trusted_anchor_txn.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(candidate_body, encoding="utf-8")
        temporary_paths.extend([candidate_path, input_path])
        from scope_signal_delta import build_issue_edit_txn_input

        # The readiness checker exposes a richer result for callers, while
        # edit_issue_txn.py intentionally accepts only its closed forwarding
        # payload. Keep this narrowing transaction-local so the controlled
        # executor's input validation remains strict.
        readiness_forwarding = {
            key: readiness[key]
            for key in (
                "status",
                "body_sha256",
                "source_checks",
                "errors",
                "readiness_result_ref",
                "resolution_evidence",
            )
            if key in readiness
        }
        transaction_input = build_issue_edit_txn_input(
            issue_number=issue_number,
            repo=repo,
            previous_body_sha256=f"sha256:{_sha256(current_issue.get('body', ''))}",
            previous_updated_at=current_issue["updatedAt"],
            new_body_file=str(candidate_path.relative_to(repo_root)),
            readiness_result=readiness_forwarding,
        )
        input_path.write_text(json.dumps(transaction_input, ensure_ascii=False), encoding="utf-8")
        transaction_script = _SCRIPTS_DIR.parent.parent / "edit-issue" / "scripts" / "edit_issue_txn.py"
        completed = subprocess.run(
            [sys.executable, str(transaction_script), "--input-file", str(input_path.relative_to(repo_root))],
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(repo_root),
            timeout=60,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"status": "failed_no_mutation"}
        return result if isinstance(result, dict) else {"status": "failed_no_mutation"}

    def fresh_checks(current_issue: dict) -> dict:
        injected = callbacks.get("fresh_checks")
        if injected is not None:
            return injected(current_issue)
        readiness = candidate_readiness(current_issue.get("body", ""))
        with contextlib.redirect_stdout(io.StringIO()):
            preflight_result, _ = run_preflight(
                issue_number=issue_number,
                repo=repo,
                anchor_comment_urls=[anchor_url],
                known_context=known_context,
                consume_contract_patch_plan=False,
            )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(current_issue.get("body", ""))
            review_body_path = Path(handle.name)
        temporary_paths.append(review_body_path)
        review_script = _SCRIPTS_DIR.parent.parent / "review-issue" / "scripts" / "check_issue_contract.py"
        review = subprocess.run(
            [sys.executable, str(review_script), "--file", str(review_body_path), "--json"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )

        # Execute the canonical Allowed Paths grammar rather than treating a
        # successful contract review as a substitute for the path gate.
        allowed_paths_status = "unavailable"
        try:
            import importlib.util

            baseline_path = (
                _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
            )
            gate_path = _SCRIPTS_DIR.parent.parent / "pr-review-judge" / "scripts" / "allowed_paths_review_gate.py"
            baseline_spec = importlib.util.spec_from_file_location("post_update_allowed_paths_baseline", baseline_path)
            gate_spec = importlib.util.spec_from_file_location("post_update_allowed_paths_gate", gate_path)
            if (
                baseline_spec is not None
                and baseline_spec.loader is not None
                and gate_spec is not None
                and gate_spec.loader is not None
            ):
                baseline_module = importlib.util.module_from_spec(baseline_spec)
                gate_module = importlib.util.module_from_spec(gate_spec)
                baseline_spec.loader.exec_module(baseline_module)
                gate_spec.loader.exec_module(gate_module)
                allowed_paths = baseline_module.extract_allowed_paths(current_issue.get("body", ""))
                normalized_paths = [
                    gate_module.AllowedPathsMatcher.normalize_allowed_pattern(path) for path in allowed_paths
                ]
                if allowed_paths and all(isinstance(path, str) and path for path in normalized_paths):
                    allowed_paths_status = "pass"
                else:
                    allowed_paths_status = "failed"
        except Exception:
            allowed_paths_status = "unavailable"

        # Validate the exact privileged command profile selected by the
        # explicit source lane.  This checks the canonical registry/policy
        # grammar, not a prose assertion from the directive.
        permission_profile_status = "unavailable"
        try:
            from command_registry import render_command

            policy_path = _find_repo_root() / "scripts" / "agent-guards" / "skill_runtime_command_policy.py"
            policy_spec = importlib.util.spec_from_file_location("post_update_runtime_policy", policy_path)
            if policy_spec is not None and policy_spec.loader is not None:
                policy_module = importlib.util.module_from_spec(policy_spec)
                # #2473: the module defines a module-level
                # `@dataclass(frozen=True)` under `from __future__ import
                # annotations`. Dataclass processing resolves those deferred
                # string annotations via `sys.modules[cls.__module__]` --
                # without registering this dynamically-loaded module there
                # BEFORE `exec_module()` runs, that lookup fails and
                # `exec_module()` raises unconditionally, which previously
                # made this gate collapse to `"unavailable"` on every call
                # regardless of the actual profile/argv validity.
                #
                # #2473 fix_delta (owner REQUEST_CHANGES on PR #2476): if
                # `exec_module()` itself raises, remove the partially
                # initialized module from `sys.modules` again before
                # propagating, matching standard import machinery semantics
                # and the existing pattern in
                # `.claude/skills/agent-retrospective/scripts/collect_snapshot.py`.
                # A failed module load must not leave a half-initialized
                # module registered for a later retry to observe.
                sys.modules[policy_spec.name] = policy_module
                try:
                    policy_spec.loader.exec_module(policy_module)
                except Exception:
                    sys.modules.pop(policy_spec.name, None)
                    raise
                context = known_context if isinstance(known_context, dict) else {}
                human_urls = _normalize_comment_url_set(context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD))
                profile = (
                    "contract_update.run.with_human_context"
                    if human_urls is not None and anchor_url in human_urls
                    else "contract_update.run.with_anchor"
                )
                # render_command proves the child argv; the policy parses the
                # matching executor argv, including the required lane flag.
                render_command(profile, {"issue_number": issue_number, "repo": repo, "anchor_comment_url": anchor_url})
                executor_argv = [
                    "uv", "run", "python3", "scripts/agent-guards/skill_runtime_exec.py",
                    "--command-id", profile, "--issue-number", str(issue_number),
                    "--repo", repo, "--anchor-comment-url", anchor_url,
                ]
                if profile == "contract_update.run.with_human_context":
                    executor_argv.extend(["--human-context-comment-url", anchor_url])
                parsed = policy_module.parse_exact_skill_runtime_contract_update_anchor_command(
                    shlex.join(executor_argv), str(_find_repo_root())
                )
                permission_profile_status = "pass" if parsed is not None else "failed"
        except Exception:
            permission_profile_status = "unavailable"

        # The rerun must retain the exact source binding in a sidecar emitted
        # by the fresh preflight.  A missing sidecar or a lane mismatch is a
        # failed runtime-evidence gate, never an implicit success.
        runtime_evidence_status = "unavailable"
        try:
            provenance_path = (
                _issue_artifact_dir(_find_repo_root(), issue_number) / "refinement_preflight_provenance_v1.json"
            )
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            source = provenance.get("runtime_evidence", {}).get("source", {})
            context = known_context if isinstance(known_context, dict) else {}
            expected_source_kind = _resolve_scope_delta_source_kind(
                anchor_url,
                human_context_comment_urls=context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD),
                agent_report_comment_urls=context.get(_AGENT_REPORT_COMMENT_URLS_FIELD),
            )
            if (
                source.get("comment_url") == anchor_url
                and source.get("source_kind") == expected_source_kind
                and provenance.get("runtime_evidence", {}).get("tested_head_sha")
            ):
                runtime_evidence_status = "pass"
            else:
                runtime_evidence_status = "failed"
        except Exception:
            runtime_evidence_status = "unavailable"
        return {
            "preflight": preflight_result.get("status"),
            "review": "approve" if review.returncode == 0 else "needs_fix" if review.returncode == 1 else "unavailable",
            "readiness": readiness.get("status"),
            "allowed_paths": allowed_paths_status,
            "permission_profile": permission_profile_status,
            "runtime_evidence": runtime_evidence_status,
        }

    normalized_anchor = dict(anchor_payload)
    normalized_anchor["html_url"] = anchor_url
    normalized_anchor["source_body_sha256"] = f"sha256:{_sha256(anchor_body)}"
    try:
        return run_trusted_anchor_iteration_zero(
            repo=repo,
            issue_number=issue_number,
            issue=issue,
            anchor=normalized_anchor,
            anchor_body=anchor_body,
            patch_plan=contract_patch_plan,
            candidate_readiness=candidate_readiness,
            fetch_current=fetch_current,
            apply_transaction=apply_transaction,
            fresh_checks=fresh_checks,
            allowed_path_deltas=_validated_allowed_path_deltas,
            scope_delta_status=(
                "approved_by_trusted_anchor" if _validated_allowed_path_deltas else None
            ),
            reflected_checker=_reflected_checker,
            patch_plan_producer_available=patch_plan_producer_available,
        )
    finally:
        for path in temporary_paths:
            try:
                path.unlink()
            except OSError:
                pass


def _satisfied_trusted_directive_noop_patch_plan(
    *,
    plan: dict[str, Any],
    issue: dict[str, Any],
    repo: str,
    issue_number: int,
    anchor_url: str,
    anchor_payload: dict[str, Any],
    anchor_body: str,
    known_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any] | None:
    """Reconstruct only a proven-satisfied trusted directive as a no-op.

    The planner intentionally omits a patch plan after it sees that a
    previously applied directive is already present.  Contract-update mode
    must not turn that safe replay into a failed mutation phase.  This helper
    is deliberately narrower than a generic missing-plan fallback: it accepts
    only the planner's ``no_scope_signal`` outcome, then independently derives
    explicit trusted operations and requires the section-aware builder to
    report no body change.
    """
    sidecar = plan.get("scope_signal_guard_decision_v2")
    if not isinstance(sidecar, dict):
        return None
    raw_signal = sidecar.get("raw_signal")
    if not isinstance(raw_signal, dict) or raw_signal.get("triggered") is not False:
        return None
    if raw_signal.get("reason_code") != "no_scope_signal":
        return None

    try:
        from scope_signal_delta import (
            build_contract_patch_plan_v1,
            build_section_aware_candidate_body,
            derive_contract_patch_operations,
            normalize_trusted_anchor_iteration_zero,
        )
    except ImportError:
        return None

    evidence = _build_scope_delta_authority_evidence(
        comment_payload=anchor_payload,
        comment_body=anchor_body,
        repo=repo,
        issue_number=issue_number,
        anchor_url=anchor_url,
        captured_at=_now_iso(),
        human_context_comment_urls=(
            known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD)
            if isinstance(known_context, dict)
            else None
        ),
        agent_report_comment_urls=(
            known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD)
            if isinstance(known_context, dict)
            else None
        ),
    )
    if (
        not isinstance(evidence, dict)
        or evidence.get("confidence") != "explicit"
        or evidence.get("source_kind") != "issue_comment"
    ):
        return None
    if evidence.get("boundary_flags"):
        return None
    normalized = normalize_trusted_anchor_iteration_zero(
        repo=repo,
        issue_number=issue_number,
        anchor=anchor_payload,
        source_body=anchor_body,
    )
    if not normalized.get("accepted"):
        return None
    operations = derive_contract_patch_operations([evidence], source_body=anchor_body)
    if not operations:
        return None
    candidate = build_section_aware_candidate_body(
        body=issue.get("body", ""),
        operations=operations,
        source_identity=normalized["source_identity"],
    )
    if candidate.get("changed"):
        return None
    return build_contract_patch_plan_v1(
        target_issue_number=issue_number,
        base_issue_body_sha256=_sha256(issue.get("body", "")),
        source_evidence=[evidence],
        operations=operations,
    )


# ---------------------------------------------------------------------------
# Structural (template-derived) repair -- local-only issue_kind/template
# resolution (Issue #995 fix_delta P0-1)
#
# `build_structural_repair_bundle()` requires a TRUSTED `issue_kind` (it is
# used to select the required-key/required-section set the Issue body is
# diffed against) plus the matching `.github/ISSUE_TEMPLATE/*.yml` text. This
# repo ships exactly one template file per issue_kind (parent/implementation/
# research), all checked in, so resolving the template never needs a GitHub
# call -- only `repo_root`. Resolving a TRUSTED issue_kind without an
# external signal is genuinely ambiguous for a body whose
# machine-readable-contract section is entirely missing/malformed (that is
# exactly the defect structural-repair exists to catch), so this function
# fails closed to (None, None, None) rather than guessing: the caller then
# leaves `structural_repair_action` absent for that run (schema-documented
# as "absent when the producer was not invoked in structural-repair mode"),
# instead of silently fabricating a `no_missing_fields_detected` or
# `human_review_required` verdict from an unresolvable issue_kind. When the
# body's self-declared `issue_kind` IS present, `detect_missing_template_
# sections()` independently re-validates it against the MRC's own parsed
# `issue_kind` key (`issue_kind_authority_conflict`), so a body that lies
# about its own kind is still caught downstream, not trusted blindly here.
# ---------------------------------------------------------------------------

_STRUCTURAL_ISSUE_KIND_RE = re.compile(r"(?m)^[ \t]*issue_kind:[ \t]*([A-Za-z0-9_-]+)[ \t]*$")
_KNOWN_STRUCTURAL_ISSUE_KINDS = frozenset({"parent", "implementation", "research"})


def _structural_deadlock_eligible_blocker(
    blocker: str, structural_repair_action: "dict | None"
) -> bool:
    """Issue #2598: shared predicate used by BOTH
    `_structural_deadlock_override_eligible()`'s per-blocker allowlist check
    and the post-override blocker cleanup at the structural-repair routing
    call site, so the two stay in sync by construction rather than by
    convention (they mirrored the same literal reason-code condition in two
    separate places before this extraction; this is a closed, narrow
    predicate over specific literal reason-code strings, not a new generic
    policy framework).

    `missing_required_section`[`:*`], `structural_repair_action_deferred:*`,
    and the bare `PLANNER_FAIL_CLOSED` companion marker are eligible
    unconditionally (unchanged #2396 behavior; see #2180's incident report
    for why the companion marker is not an unrelated blocker namespace).

    The bare parent-specific `missing_required_parent_section` reason code
    is ALSO eligible, but ONLY when the structural bundle being evaluated
    is itself independently resolved as `issue_kind: parent` -- the blocker
    STRING alone is never trusted to imply the bundle's own shape. A
    SUFFIXED form (`missing_required_parent_section:*`) does NOT count,
    matching the existing bare-code-only precedent already established for
    `PLANNER_FAIL_CLOSED`.
    """
    if (
        blocker == "missing_required_section"
        or blocker.startswith("missing_required_section:")
        or blocker.startswith("structural_repair_action_deferred:")
        or blocker == BLOCKER_FAIL_CLOSED
    ):
        return True
    if blocker == "missing_required_parent_section":
        return (
            isinstance(structural_repair_action, dict)
            and structural_repair_action.get("issue_kind") == "parent"
        )
    return False


def _structural_deadlock_override_eligible(
    structural_repair_action: "dict | None",
    blockers: list[str],
    required_sections: list[str],
    required_contract_keys: list[str],
) -> bool:
    """Issue #2396: eligibility check for overriding the severity-arbitration
    `structural_repair_action_deferred` suppression when the incoming
    structural candidate would itself resolve the SAME
    `missing_required_section` blocker(s) that made the pre-existing status
    more severe than the structural target (the #2180 blocked-precedence
    deadlock). Independently re-validates every condition against the
    bundle's own items -- never trusts `disposition_summary` alone --
    mirroring `route_structural_repair_disposition()`'s own defense-in-depth
    stance (a summary claiming auto_apply_safe must be backed by items that
    are themselves auto_apply_safe).

    All of the following must hold, or this returns False (existing defer
    behavior is preserved):
      1. every item's own `disposition` is `auto_apply_safe`
      2. every item's `insertion.disposition` is exactly `"exact"` (a
         positive allowlist, not merely "not ambiguous" -- a producer bug,
         a hand-crafted/adversarial artifact, or a future non-`"ambiguous"`
         disposition value could otherwise smuggle an unanchorable item
         through under a false auto_apply_safe summary)
      3. the bundle's own covered targets (whole-section `label`s plus
         Machine-Readable-Contract key names) are EXACTLY the union of
         `required_sections`/`required_contract_keys` (neither more nor
         less) -- when that union is empty, coverage can never be
         established from the anonymous `missing_required_section` planner
         blocker alone, so the override never fires
      4. `blockers` contains ONLY entries accepted by
         `_structural_deadlock_eligible_blocker()` -- `missing_required_section`
         [`:*`], `structural_repair_action_deferred:*`, the bare
         `PLANNER_FAIL_CLOSED` companion marker, and (Issue #2598) the bare
         `missing_required_parent_section` code when this bundle's own
         `issue_kind` is `parent` -- any other blocker (allowed_paths,
         secret, environment, etc.) keeps the existing blocked status
         untouched
    """
    if not isinstance(structural_repair_action, dict):
        return False
    items = structural_repair_action.get("items")
    if not isinstance(items, list) or not items:
        return False

    covered_sections: set[str] = set()
    covered_contract_keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("disposition") != STRUCT_DISPOSITION_AUTO_APPLY_SAFE:
            return False
        insertion = item.get("insertion")
        if not isinstance(insertion, dict) or insertion.get("disposition") != "exact":
            return False
        field_id = item.get("field_id")
        if isinstance(field_id, str) and field_id.startswith("machine-readable-contract."):
            covered_contract_keys.add(field_id.split(".", 1)[1])
        else:
            label = item.get("label")
            if isinstance(label, str):
                covered_sections.add(label)

    required_targets = set(required_sections or []) | set(required_contract_keys or [])
    covered_targets = covered_sections | covered_contract_keys
    if not required_targets or required_targets != covered_targets:
        return False

    for _b in blockers:
        if _structural_deadlock_eligible_blocker(_b, structural_repair_action):
            continue
        return False
    return True


def _resolve_structural_repair_template(
    body: str, repo_root: Path
) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """Best-effort, LOCAL-ONLY (no GitHub call) resolution of which
    `.github/ISSUE_TEMPLATE/<kind>.yml` a structural-repair pass should diff
    the Issue body against.

    Returns (issue_kind, template_text, template_relpath) or
    (None, None, None) when the body has no resolvable self-declared
    `issue_kind` scalar, the declared value is not one of the closed
    {parent, implementation, research} kinds, or the corresponding template
    file cannot be read from repo_root.
    """
    match = _STRUCTURAL_ISSUE_KIND_RE.search(body or "")
    if match is None:
        return None, None, None
    candidate_kind = match.group(1).strip()
    if candidate_kind not in _KNOWN_STRUCTURAL_ISSUE_KINDS:
        return None, None, None
    template_relpath = f".github/ISSUE_TEMPLATE/{candidate_kind}.yml"
    template_path = repo_root / template_relpath
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except OSError:
        return None, None, None
    return candidate_kind, template_text, template_relpath


# ---------------------------------------------------------------------------
# Structural repair known_scalars / source_spans assembler (Issue #2431)
#
# Wires `detect_missing_template_sections()`'s (repair_issue_contract.py)
# `known_scalars`/`source_spans` parameters -- previously never populated by
# this production preflight path, so a derivation-eligible missing section
# fell through to `human_review_required` even when it was actually safely
# derivable (#2426's manifestation) -- from data ALREADY available on this
# SAME preflight run: the current Issue body's own Machine-Readable Contract
# / headings, and (only when the caller has already established the anchor
# comment is a TRUSTED human-context comment via the EXISTING
# `_determine_repair_source_lane()`/`_resolve_scope_delta_source_kind()`
# lane classifier -- no new trusted-anchor judgment logic is added here) the
# anchor comment body. Never performs a new GitHub read of a DIFFERENT
# Issue: the structural-repair pass stays local-only, matching
# `_resolve_structural_repair_template()`'s own local-only design above.
# ---------------------------------------------------------------------------

# Issue #2431 AC2: the closed pair of consumer field-ids a resolved
# `parent_issue` scalar is ever cross-populated into -- the "## Parent
# Issue" template heading field and the Machine-Readable Contract's own
# `parent_issue` key. Both represent the SAME real-world value.
_PARENT_ISSUE_KNOWN_SCALAR_FIELD_IDS = ("parent-issue", "machine-readable-contract.parent_issue")

# Issue #2431 AC5: closed, one-way, EXACT heading alias table for the
# `current_issue` same-body `source_span_exact` authority
# (repair_issue_contract.py's `_SOURCE_SPAN_AUTHORITY_KINDS` additive
# extension). Only this ONE pair -- never fuzzy/substring/semantic matching,
# never any other heading pair.
_STRUCTURAL_HEADING_ALIAS_TABLE: dict[str, str] = {
    "Proposed Allowed Paths": "Allowed Paths",
}
# Canonical heading label (the alias table's VALUE side) -> template
# field_id, so the assembler below can key its `source_spans` dict by the
# SAME field_id `detect_missing_template_sections()` uses.
_STRUCTURAL_HEADING_LABEL_TO_FIELD_ID: dict[str, str] = {
    "Allowed Paths": "allowed-paths",
}


def _resolve_parent_issue_known_scalar(
    body: str,
    *,
    trusted_anchor_body: "str | None" = None,
) -> "str | None":
    """Resolve a single `parent_issue` scalar (Issue #2431 AC2) cross-
    populatable into both `_PARENT_ISSUE_KNOWN_SCALAR_FIELD_IDS`, from up to
    3 candidate sources: the current Issue body's own Machine-Readable
    Contract `parent_issue` key, its own `## Parent Issue` heading section,
    and -- ONLY when `trusted_anchor_body` is supplied (the caller must
    already have established this via the existing human-context lane
    classifier; this function performs no trust judgment of its own) -- an
    anchor comment `## Parent Issue` heading section.

    Every candidate is validated with repair_issue_contract.py's OWN closed
    `machine-readable-contract.parent_issue` validator regex (never a
    locally re-declared pattern). Zero candidates, or 2+ candidates with
    DISTINCT values, both resolve to `None` (fail-closed) -- this function
    never guesses between conflicting sources.
    """
    if _mrc_parse is None or _parse_h2_sections is None:  # pragma: no cover - defensive
        return None
    validator = _DERIVED_SCALAR_FIELD_VALIDATORS.get("machine-readable-contract.parent_issue")
    if validator is None:  # pragma: no cover - defensive, validator always registered
        return None

    candidates: set[str] = set()

    mrc_result = _mrc_parse(body)
    if mrc_result.get("status") == MRC_PARSE_STATUS_OK:
        raw = mrc_result["keys"].get("parent_issue")
        if isinstance(raw, str) and validator.match(raw.strip()):
            candidates.add(raw.strip())

    def _add_heading_candidate(text: str) -> None:
        for section in _parse_h2_sections(text):
            if section["heading"].strip().casefold() == "parent issue":
                raw = section["content"].strip()
                if validator.match(raw):
                    candidates.add(raw)
                break

    _add_heading_candidate(body)
    if trusted_anchor_body:
        _add_heading_candidate(trusted_anchor_body)

    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _resolve_current_issue_heading_alias_source_spans(
    body: str,
    *,
    repo: str,
    issue_number: int,
) -> "dict[str, dict[str, Any]]":
    """Detect the closed `_STRUCTURAL_HEADING_ALIAS_TABLE` heading alias
    (Issue #2431 AC4/AC5) in the CURRENT Issue body and, only when every
    precondition below holds, return a `source_span_exact` candidate
    (`authority_kind: current_issue`) keyed by the canonical field's
    field_id:

      - the alias heading is present EXACTLY ONCE in `body`
      - the canonical heading is NOT already present in `body`
      - the alias section has non-empty content

    `source_revision` is set to `sha256(body)` -- since `text` is read
    directly out of this EXACT in-memory `body` (the same `body` this
    producer run's `original_body_sha256` is computed from), a downstream
    consumer can independently confirm `source_revision ==
    "sha256:" + item["original_body_sha256"]` never crossed a stale/
    different body snapshot (AC4's same-body / digest-match precondition).

    PR #2469 fix_delta iteration 3 (P1): `line_start`/`line_end` are NOT
    simply "the line right after the heading" -- ordinary Markdown puts a
    blank separator line between a `## Heading` and its content (heading ->
    blank line -> bullets), and `_parse_h2_sections()`'s `content` field is
    `.strip()`-ed, so the FIRST line of `content` is offset from the raw
    section start by however many leading blank lines precede it (and the
    LAST line is likewise offset by trailing blank lines the strip()
    removed). This function locates the actual raw line range the
    `.strip()`-ed `content` occupies -- using the SAME
    `_section_line_bounds()` helper `_apply_insertion_decision()` uses for
    the identical raw-line-range problem, never a re-derived offset -- so
    `body.splitlines()[line_start - 1:line_end]` always reconstructs
    exactly `content`.
    """
    if _parse_h2_sections is None or _section_line_bounds is None:  # pragma: no cover - defensive
        return {}

    sections = _parse_h2_sections(body)
    heading_index: dict[str, list[dict]] = {}
    for section in sections:
        heading_index.setdefault(section["heading"].strip().casefold(), []).append(section)

    body_lines = body.split("\n")
    bounds = _section_line_bounds(sections, len(body_lines))

    body_digest = "sha256:" + _sha256(body)
    source_url = f"https://github.com/{repo}/issues/{issue_number}"
    spans: dict[str, dict[str, Any]] = {}
    for alias_heading, canonical_heading in _STRUCTURAL_HEADING_ALIAS_TABLE.items():
        field_id = _STRUCTURAL_HEADING_LABEL_TO_FIELD_ID.get(canonical_heading)
        if field_id is None:  # pragma: no cover - defensive, table always in sync
            continue
        alias_matches = heading_index.get(alias_heading.strip().casefold(), [])
        canonical_matches = heading_index.get(canonical_heading.strip().casefold(), [])
        if len(alias_matches) != 1 or len(canonical_matches) != 0:
            continue
        alias_section = alias_matches[0]
        alias_content = alias_section["content"].strip()
        if not alias_content:
            continue
        # Raw (pre-strip) line range of this section's content: the line
        # right after the heading through the line just before the next
        # section (or end of body) -- the SAME range `_parse_h2_sections()`
        # itself joined (pre-strip) to produce `content`.
        _heading_start, section_end_line = bounds[id(alias_section)]
        raw_content_start_line = alias_section["start_line"] + 1
        if raw_content_start_line > section_end_line:  # pragma: no cover - defensive, empty section
            continue
        raw_content_lines = body_lines[raw_content_start_line - 1 : section_end_line]
        raw_content_joined = "\n".join(raw_content_lines)
        leading_ws_len = len(raw_content_joined) - len(raw_content_joined.lstrip())
        leading_blank_lines = raw_content_joined[:leading_ws_len].count("\n")
        line_start = raw_content_start_line + leading_blank_lines
        line_end = line_start + max(0, alias_content.count("\n"))
        # Regression guard (PR #2469 fix_delta iteration 3, P1): fail-closed
        # rather than emit a `source_span_exact` candidate whose line range
        # does not byte-exactly reconstruct `text` -- a downstream consumer
        # trusts `line_start`/`line_end` without re-deriving them.
        if "\n".join(body_lines[line_start - 1 : line_end]) != alias_content:  # pragma: no cover - defensive
            continue
        spans[field_id] = {
            "text": alias_content,
            "source_url": source_url,
            "line_start": line_start,
            "line_end": line_end,
            "authority_kind": "current_issue",
            "source_repo": repo,
            "source_object_kind": "issue_body",
            "source_object_id": str(issue_number),
            "source_revision": body_digest,
        }
    return spans


# Issue #2582: closed mapping of pre-existing parent-template required fields
# that may be copied from an explicit human-context OWNER anchor. This is not a
# new authority, derivation mode, or template profile: it only binds existing
# template field ids to their exact canonical H2 labels.
_OWNER_ANCHOR_REQUIRED_SECTION_FIELDS: dict[str, str] = {
    "Quality Decision Record": "quality-decision-record",
    "Child Issues": "child-issues",
    "Remaining Parent Gaps": "remaining-parent-gaps",
}


def _resolve_owner_anchor_required_section_source_spans(
    target_body: str,
    *,
    anchor_body: str,
    anchor_url: str,
    anchor_comment: dict[str, Any],
    repo: str,
    issue_number: int,
) -> "dict[str, dict[str, Any]]":
    """Return exact parent-template section spans from one trusted OWNER
    comment, or no spans at all when any source binding is invalid.

    The caller has already selected the explicit ``human_context`` lane. This
    helper adds the narrower producer-side binding required for the existing
    ``owner_anchor`` authority: exact comment URL/id/repository/target/body
    snapshot and revision, followed by fence-aware exact-H2 extraction. It
    deliberately does not infer human origin from a URL and never accepts a
    MEMBER/COLLABORATOR/agent anchor for this owner-only lane.
    """
    if _parse_h2_sections is None or _section_line_bounds is None:
        return {}
    if not isinstance(anchor_comment, dict) or not isinstance(anchor_body, str) or not anchor_body:
        return {}
    parsed_url = _parse_anchor_comment_url(anchor_url)
    if (
        not parsed_url.get("valid")
        or f"{parsed_url.get('owner')}/{parsed_url.get('repo')}" != repo
        or parsed_url.get("issue_number") != issue_number
        or str(anchor_comment.get("id")) != str(parsed_url.get("comment_id"))
        or anchor_comment.get("html_url") != anchor_url
        or anchor_comment.get("issue_url") != f"https://api.github.com/repos/{repo}/issues/{issue_number}"
        or anchor_comment.get("author_association") != "OWNER"
        or anchor_comment.get("body") != anchor_body
        or not isinstance(anchor_comment.get("updated_at"), str)
        or not anchor_comment["updated_at"].strip()
    ):
        return {}

    target_sections = _parse_h2_sections(target_body)
    anchor_sections = _parse_h2_sections(anchor_body)
    target_index: dict[str, list[dict]] = {}
    anchor_index: dict[str, list[dict]] = {}
    for section in target_sections:
        target_index.setdefault(section["heading"].strip().casefold(), []).append(section)
    for section in anchor_sections:
        anchor_index.setdefault(section["heading"].strip().casefold(), []).append(section)

    anchor_lines = anchor_body.split("\n")
    anchor_bounds = _section_line_bounds(anchor_sections, len(anchor_lines))
    source_revision = "sha256:" + _sha256(anchor_body)
    spans: dict[str, dict[str, Any]] = {}
    for heading, field_id in _OWNER_ANCHOR_REQUIRED_SECTION_FIELDS.items():
        normalized = heading.casefold()
        target_matches = target_index.get(normalized, [])
        anchor_matches = anchor_index.get(normalized, [])
        # A trusted owner source must be the literal canonical H2 at column
        # zero. Keep the generic parser for fence-aware section bounds, but
        # never let its CommonMark-compatible indented/trailing-hash forms
        # qualify as this narrower authority's canonical candidate.
        if (
            len(target_matches) != 0
            or len(anchor_matches) != 1
            or anchor_lines[anchor_matches[0]["start_line"] - 1] != f"## {heading}"
        ):
            continue
        anchor_section = anchor_matches[0]
        content = anchor_section["content"].strip()
        if not content:
            continue
        _heading_start, section_end_line = anchor_bounds[id(anchor_section)]
        raw_content_start_line = anchor_section["start_line"] + 1
        if raw_content_start_line > section_end_line:
            continue
        raw_content = "\n".join(anchor_lines[raw_content_start_line - 1 : section_end_line])
        leading_ws_len = len(raw_content) - len(raw_content.lstrip())
        line_start = raw_content_start_line + raw_content[:leading_ws_len].count("\n")
        line_end = line_start + content.count("\n")
        if "\n".join(anchor_lines[line_start - 1 : line_end]) != content:
            continue
        spans[field_id] = {
            "text": content,
            "source_url": anchor_url,
            "line_start": line_start,
            "line_end": line_end,
            "authority_kind": "owner_anchor",
            "source_repo": repo,
            "source_object_kind": "issue_comment",
            "source_object_id": str(parsed_url["comment_id"]),
            "source_revision": source_revision,
        }
    return spans


def run_preflight(
    issue_number: int,
    repo: str,
    anchor_comment_urls: list[str],

    fixture_path: Optional[Path] = None,
    known_context: Optional[dict] = None,
    now: Optional[str] = None,
    consume_contract_patch_plan: bool = False,
    contract_update_callbacks: Optional[dict[str, Any]] = None,
    investigation_evidence_transport_path: "Optional[Path]" = None,
    investigation_evidence_primary_root: "Optional[Path]" = None,
    enable_main_drift_live_readback: bool = False,
    evidence_cache_enabled: bool = False,
    evidence_index: "object | None" = None,
) -> tuple[dict, int]:
    """
    Main preflight logic.

    Returns (result_dict, exit_code).
    Writes artifacts and prints compact stdout.

    Artifact write guarantee: stdout and disk are written from the same final
    result dict (no post-write mutation). This ensures AC6 failure-path consistency.

    `evidence_cache_enabled` (Issue #2052, default False -- opt-in, zero
    behavior change for every pre-existing caller/test): when True, the
    live-mode Issue/comment fetch section below routes through a single
    `EvidenceIndex` scoped to the `EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH`
    phase of THIS invocation only. This never changes `issue`/`comments`
    content, `next_action`, `blockers`, or any other decision field --
    enabling/disabling it is required (AC6) to be semantically
    equivalent, differing only in the observed `fetch_count` /
    `emitted_utf8_bytes` metrics written to a NEW, purely-additive
    `context_budget_report.json` artifact (never one of the three
    existing fixed-schema artifacts). A missing `evidence_index`/
    `context_budget_report` import (e.g. a harness that only provisions
    this skill's own `scripts/` directory) degrades this to a no-op --
    every fetch stays fresh, exactly like `evidence_cache_enabled=False`.

    `evidence_index` (Issue #2052, default None): an already-constructed
    `EvidenceIndex` the caller wants THIS invocation to share (e.g. an
    integration test that also calls `_validate_anchor_comments_batch()`
    directly, afterward, with the same instance, to observe reuse across
    both call sites within one conceptual phase). Only consulted when
    `evidence_cache_enabled=True` -- passing an instance without also
    setting that flag has no effect (the flag is the single opt-in gate;
    fixture mode never uses a passed-in instance either, since fixture mode
    never performs a live read at all).
    """
    repo_root = _find_repo_root()
    blockers: list[str] = []
    planner_exit_code: Optional[int] = None
    planner_fail_closed: Optional[bool] = None
    planner_fail_closed_reason_codes: list[str] = []
    required_sections: list[str] = []
    required_contract_keys: list[str] = []
    rewrite_constraints: Optional[dict[str, Any]] = None
    planner_input_dict: Optional[dict] = None
    raw_snapshot: Optional[dict] = None
    anchor_payload_for_consumer: Optional[dict] = None
    anchor_body_for_consumer: Optional[str] = None
    anchor_url_for_consumer: Optional[str] = None
    contract_update_handoff: Optional[dict[str, Any]] = None
    # #2048 Scope Delta: True when the trusted-anchor consumer detected a
    # full_rewrite_required disposition (approved scope reframe, empty
    # operations[], not yet reflected in the current body). Overrides
    # next_action to issue_editor_required and keeps this transition out of
    # the ordinary contract-update success/failure blocker path.
    contract_update_route_issue_editor_required: bool = False

    # Issue #2257 P0-1: `transport` is `None` in fixture mode (unused --
    # fixture mode never performs a live read at all) and is set to the
    # single, explicitly-selected read authority in live mode below.
    transport: "object | None" = None
    # Issue #2052: `evidence_index` (the function parameter, default None)
    # is forced back to `None` in fixture mode below (fixture mode never
    # performs a live read, so there is nothing to cache -- a caller-passed
    # instance must never leak in there) and is only actually consulted in
    # live mode, and only when `evidence_cache_enabled=True`.

    # --- Load data (fixture or live gh) ---
    if fixture_path is not None:
        evidence_index = None
        # Fixture mode: load pre-fetched snapshot
        try:
            fixture_raw = fixture_path.read_text(encoding="utf-8")
            fixture_data = json.loads(fixture_raw)
        except Exception as exc:
            result = _build_result(
                status="environment_failure",
                issue_number=issue_number,
                repo=repo,
                planner_exit_code=None,
                planner_fail_closed=None,
                next_action="fix_environment",
                must_read=[],
                do_not_read=[],
                commands=[],
                blockers=[f"FIXTURE_LOAD_ERROR: {exc}"],
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
                artifacts={},
                hashes={},
                environment_failure_reason_code="transport_internal_error",
                environment_failure_source="internal",
                environment_failure_operation="load_fixture",
            )
            print(_build_compact_stdout(result))
            return result, EXIT_ENVIRONMENT_FAILURE

        # Validate fixture input against input schema (fail-closed on unknown input)
        input_schema = _load_schema("refinement_preflight_input.schema.json")
        if input_schema is not None:
            is_valid, schema_errors = _validate_with_schema(fixture_data, input_schema)
            if not is_valid:
                err_detail = "; ".join(schema_errors)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="blocked",
                    next_action="human_judgment_required",
                    blockers=[BLOCKER_INPUT_SCHEMA_INVALID, f"input_schema_errors: {err_detail}"],
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                )

        # The fixture stands in for a live read of `repo`; a snapshot that declares a
        # different (or no) repository must never be planned as if it came from `repo`.
        if fixture_data.get("repo") != repo:
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="blocked",
                next_action="human_judgment_required",
                blockers=[BLOCKER_REPOSITORY_IDENTITY_MISMATCH],
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        issue = fixture_data.get("issue", {})
        comments = fixture_data.get("comments", [])
        fixture_anchor_comments = fixture_data.get("anchor_comments", [])
        fixture_anchor_urls = fixture_data.get("anchor_comment_urls", anchor_comment_urls)

        # Use fixture anchor data for structural validation
        active_anchor_urls = fixture_anchor_urls or anchor_comment_urls
        fixture_comment_lookup = fixture_anchor_comments if fixture_anchor_comments else comments
        known_context = known_context or fixture_data.get("known_context")
        now = now or fixture_data.get("now")
    else:
        # Live mode: fetch from GitHub via a single, explicitly-selected
        # read authority (Issue #2257 P0-1). `transport` is selected exactly
        # ONCE here and threaded through every subsequent read on this
        # invocation's critical path -- `_fetch_issue`, `_fetch_issue_comments`,
        # and (only for the fresh-readback/TOCTOU fallback below)
        # `_fetch_single_comment` all resolve this SAME instance (Issue
        # #2872: native `gh` always; no HOME/auth/failure-dependent
        # re-selection, so no read can switch to an anonymous authority).
        _reset_read_transport_cache()
        transport = _select_read_transport()
        transport_source = getattr(transport, "SOURCE_LABEL", "internal")

        # Issue #2052 (opt-in, default off): a single EvidenceIndex scoped
        # to EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH for this invocation only.
        # `evidence_index` (the function parameter) is forced back to None
        # -- a pure passthrough below -- unless the caller explicitly
        # requested the cache AND the shared module imported successfully;
        # the flag is the single opt-in gate, so a caller-passed instance
        # without `evidence_cache_enabled=True` is never consulted. When an
        # instance IS passed in, it is reused as-is (letting a caller, e.g.
        # an integration test, observe reuse across this call and a
        # subsequent direct call to `_validate_anchor_comments_batch()`
        # sharing the same instance) rather than always constructing a
        # fresh one.
        if evidence_cache_enabled and _EvidenceIndex is not None:
            if evidence_index is None:
                evidence_index = _EvidenceIndex()
            evidence_index.begin_phase(EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH)
        else:
            evidence_index = None

        # NOTE: `_fetch_issue`/`_fetch_issue_comments` are called WITHOUT an
        # explicit `transport=` keyword here (unlike `_fetch_single_comment`
        # below) so that pre-existing unit tests which monkeypatch these two
        # functions with a fixed 2-positional-argument signature
        # (`test_main_drift_evidence_epoch.py`, outside this Issue's Allowed
        # Paths) keep working. Single-authority is still guaranteed: both
        # functions resolve their transport via `_select_read_transport()`,
        # which is a constant (Issue #2872) and therefore returns the EXACT
        # SAME `transport` instance already selected on the line above.
        if evidence_index is not None:
            _issue_outcome = evidence_index.get_or_fetch(
                repository=repo,
                resource_kind=_EVIDENCE_RESOURCE_KIND_ISSUE_BODY,
                resource_id=issue_number,
                fetch_fn=lambda: _fetch_issue(repo, issue_number),
            )
            issue, err = _issue_outcome.raw_snapshot, (_issue_outcome.err or "")
        else:
            issue, err = _fetch_issue(repo, issue_number)
        if issue is None:
            blockers.append(BLOCKER_GH_FAILURE)
            reason_code = _project_environment_failure_reason(err)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="environment_failure",
                next_action="fix_environment",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
                environment_failure_reason_code=reason_code,
                environment_failure_source=transport_source,
                environment_failure_operation="read_issue",
            )

        comments, err = _fetch_issue_comments(repo, issue_number)
        if comments is None:
            blockers.append(BLOCKER_GH_FAILURE)
            reason_code = _project_environment_failure_reason(err)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="environment_failure",
                next_action="fix_environment",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
                environment_failure_reason_code=reason_code,
                environment_failure_source=transport_source,
                environment_failure_operation="list_issue_comments",
            )

        active_anchor_urls = anchor_comment_urls
        # Issue #2257 P0-1 (the decisive fix): the initial anchor comment
        # MUST be resolved from the complete paginated comments traversal
        # already performed above (`comments`), never from a second,
        # independent single-comment GET. Passing `comments` here as the
        # `fixture_comments` lookup for `_validate_anchor_comments_batch` /
        # `_validate_anchor_comment_url` means the live-mode "else" branch
        # in `_validate_anchor_comment_url` (a fresh single-comment fetch)
        # is reached only when `comments` unexpectedly does not contain the
        # anchor id at all -- never as the normal initial-anchor-resolution
        # path. Before this fix, `fixture_comment_lookup` was hardcoded to
        # `None` here even though `comments` had already been fetched,
        # forcing every live-mode anchor resolution through a redundant
        # single-comment GET -- the structural split-brain surface Issue
        # #2257 exists to close.
        fixture_comment_lookup = comments

    anchor_comment_state: Optional[dict[str, Any]] = None
    anchor_comment_feedback: Optional[dict[str, Any]] = None
    anchor_comment_ids: set[str] = set()

    # --- Anchor comment structural validation ---
    if active_anchor_urls:
        sorted_urls, anchor_blockers = _validate_anchor_comments_batch(
            active_anchor_urls,
            repo,
            issue_number,
            fixture_comments=fixture_comment_lookup,
            transport=transport,
            evidence_index=evidence_index,
        )
        if anchor_blockers:
            blockers.extend(anchor_blockers)
            # Issue #2257 AC3: a transport failure resolving the anchor
            # comment (auth dependency, rate limit, 5xx, DNS/connection,
            # timeout, malformed JSON, incomplete pagination, read
            # authority divergence) is reported as environment_failure --
            # never as the semantic "blocked/human_judgment_required" path
            # that ANCHOR_COMMENT_NOT_FOUND takes for a true 404.
            if BLOCKER_ANCHOR_FETCH_TRANSPORT_FAILURE in anchor_blockers:
                anchor_reason_code = "transport_internal_error"
                for _b in anchor_blockers:
                    if _b.startswith("ANCHOR_FETCH_REASON:"):
                        anchor_reason_code = _b[len("ANCHOR_FETCH_REASON:"):]
                        break
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="environment_failure",
                    next_action="fix_environment",
                    blockers=blockers,
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                    environment_failure_reason_code=anchor_reason_code,
                    environment_failure_source=(
                        transport.SOURCE_LABEL
                        if transport is not None and hasattr(transport, "SOURCE_LABEL")
                        else "internal"
                    ),
                    environment_failure_operation="read_issue_comment",
                )
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="blocked",
                next_action="human_judgment_required",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        if sorted_urls:
            anchor_url = sorted_urls[0]
            parsed_anchor = _parse_anchor_comment_url(anchor_url)
            comment_id = parsed_anchor.get("comment_id")
            # Issue #2052 fix_delta A: this re-resolution of the SAME
            # `comment_id` that `_validate_anchor_comments_batch()` (via
            # `_validate_anchor_comment_url()`) already resolved just above
            # now routes through the SAME shared `_resolve_anchor_comment_payload()`
            # helper -- when `evidence_index` is supplied, this call is a
            # cache HIT (no second list scan / no second GET); when it is
            # not supplied (`evidence_cache_enabled=False`, the pre-existing
            # default), behavior is byte-for-byte identical to before: a
            # fresh list scan when `fixture_comment_lookup` is available, or
            # a fresh single-comment GET otherwise.
            comment_payload, err = _resolve_anchor_comment_payload(
                comment_id=comment_id,
                repo=repo,
                fixture_comments=fixture_comment_lookup,
                transport=transport,
                evidence_index=evidence_index,
            )
            if fixture_comment_lookup is None and comment_payload is None:
                # Issue #2257 (low-priority follow-up noted alongside the
                # AC1-AC3 fix): keep this re-fetch's err classification
                # consistent with `_validate_anchor_comment_url` above
                # instead of silently discarding it -- a true
                # semantic_missing here (the comment vanished between the
                # batch validation above and this re-fetch) still gets a
                # distinguishing blocker, not just a generic GH_API_FAILURE.
                blockers.append(BLOCKER_GH_FAILURE)
                if not _is_transport_failure(err):
                    blockers.append(BLOCKER_ANCHOR_COMMENT_NOT_FOUND)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="environment_failure",
                    next_action="fix_environment",
                    blockers=blockers,
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                    environment_failure_reason_code=_project_environment_failure_reason(err),
                    environment_failure_source=(
                        transport.SOURCE_LABEL
                        if transport is not None and hasattr(transport, "SOURCE_LABEL")
                        else "internal"
                    ),
                    environment_failure_operation="read_issue_comment",
                )

            anchor_comment_state, anchor_errors = _build_anchor_comment_state(
                anchor_url=anchor_url,
                comment=comment_payload,
                issue_number=issue_number,
                captured_at=now or _now_iso(),
            )
            if anchor_errors:
                blockers.extend(anchor_errors)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="blocked",
                    next_action="human_judgment_required",
                    blockers=blockers,
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                )

            anchor_comment_ids.add(str(comment_payload["id"]))
            anchor_payload_for_consumer = dict(comment_payload)
            anchor_body_for_consumer = anchor_comment_state["snapshot"]
            anchor_url_for_consumer = anchor_url
            anchor_comment_feedback = {
                "url": anchor_comment_state["url"],
                "preliminary_classification": anchor_comment_state["preliminary_classification"],
                "final_classification": anchor_comment_state["final_classification"],
                "classification_reason": anchor_comment_state["classification_reason"],
                "verified_claims": anchor_comment_state["verified_claims"],
                "unresolved_claims": anchor_comment_state["unresolved_claims"],
                "scope_impact": anchor_comment_state["scope_impact"],
                "requires_fact_check": anchor_comment_state["requires_fact_check"],
            }

            # --- Classify ANCHOR_SCOPE_REFRAME_V1 and build scope_delta_decision ---
            scope_delta_decision = _classify_anchor_scope_reframe(
                comment_payload=comment_payload,
                anchor_body=anchor_comment_state["snapshot"],
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url,
            )
            # Propagate to known_context so planner sees anchor_reframe context
            _kc = dict(known_context) if known_context else {}
            # The iteration-zero consumer must cross-check normalized anchor
            # evidence against the live repository even though planner input
            # intentionally omits raw issue URLs.
            _kc["repo"] = repo
            # NOTE(PR #1973 P0-1 fix_delta): anchor_reframe is intentionally
            # NOT set here from the pre-route `scope_delta_decision`. It is
            # computed further below, AFTER `_apply_multi_turn_candidate_route()`
            # runs, from the FINAL routed status -- otherwise it can go stale
            # / contradict the routed decision (split-brain).
            _kc["anchor_comment_url"] = anchor_url
            _kc["anchor_comment_hash"] = scope_delta_decision.get("anchor_comment_hash", "")
            _kc["scope_delta_decision"] = scope_delta_decision

            # --- #1891: anchor_context.py pure analyzer (segment + candidates) ---
            # anchor_context.py has no GitHub API client of its own (AC8); it
            # only consumes the already-fetched anchor_comment.snapshot body
            # built above. Computed here -- immediately before the
            # _build_scope_delta_authority_evidence() call below -- so both
            # this call and the multi-turn candidate route that follows it
            # share the same segment/candidate outputs at a single call site
            # (Issue #1891 In Scope: "_build_scope_delta_authority_evidence()
            # に anchor_context.py segment + candidates の出力を渡す").
            _anchor_body_for_context = anchor_comment_state["snapshot"]
            _segments_result = None
            _candidates_result = None
            if anchor_context is not None:
                _segments_result = anchor_context.segment_body(_anchor_body_for_context)
                _candidates_result = anchor_context.extract_candidates(_anchor_body_for_context)
                _kc["source_fetch_complete"] = bool(_anchor_body_for_context)
                _kc["source_hash_verified"] = _sha256(_anchor_body_for_context) == scope_delta_decision.get(
                    "anchor_comment_hash"
                )
                _kc["source_ranges_covered"] = anchor_context.compute_source_ranges_covered(
                    _segments_result["segments"], _segments_result["line_count"]
                )
            else:  # pragma: no cover - defensive fallback when import fails
                _kc["source_fetch_complete"] = bool(_anchor_body_for_context)
                _kc["source_hash_verified"] = False
                _kc["source_ranges_covered"] = False

            # --- #1323: build freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 ---
            # (independent of the structured ANCHOR_SCOPE_REFRAME_V1 payload
            # above, so explicit human-review directives in freeform review
            # comments -- e.g. Issue #1270 -- are not dropped just because
            # they carry no machine-formatted reframe marker.)
            # #2053 AC2: a structured ANCHOR_SCOPE_REFRAME_V1 payload that
            # WAS present but invalid/stale/wrong-target must never be
            # reinterpreted as freeform authority evidence from the same
            # comment body -- that would be a downgrade fallback around
            # structured schema validation. "No structured payload at all"
            # remains the legitimate freeform lane (unchanged).
            if _structured_anchor_payload_present_but_invalid(scope_delta_decision):
                _scope_delta_authority_evidence = None
            else:
                _scope_delta_authority_evidence = _build_scope_delta_authority_evidence(
                    comment_payload=comment_payload,
                    comment_body=anchor_comment_state["snapshot"],
                    repo=repo,
                    issue_number=issue_number,
                    anchor_url=anchor_url,
                    captured_at=now or _now_iso(),
                    human_context_comment_urls=(
                        known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD)
                        if isinstance(known_context, dict)
                        else None
                    ),
                    agent_report_comment_urls=(
                        known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD)
                        if isinstance(known_context, dict)
                        else None
                    ),
                    segments_result=_segments_result,
                    candidates_result=_candidates_result,
                )
            if _scope_delta_authority_evidence is not None:
                _kc["scope_delta_authority_evidence"] = [_scope_delta_authority_evidence]

            # --- #1891 AC4: route to human judgment on genuine multi-turn
            # ambiguity, using the same _segments_result / _candidates_result
            # this call site just fetched and forwarded above. ---
            _kc["scope_delta_decision"] = _apply_multi_turn_candidate_route(
                _kc["scope_delta_decision"],
                _segments_result,
                _candidates_result,
                integrity_predicates={
                    "source_fetch_complete": _kc.get("source_fetch_complete"),
                    "source_hash_verified": _kc.get("source_hash_verified"),
                    "source_ranges_covered": _kc.get("source_ranges_covered"),
                },
            )

            # PR #1973 (OWNER REQUEST_CHANGES, P0-1): anchor_reframe is
            # computed from the FINAL routed status (post-route), so it never
            # contradicts `_kc["scope_delta_decision"]["status"]`.
            _kc["anchor_reframe"] = _kc["scope_delta_decision"].get("status") == "approved_by_trusted_anchor"

            # --- #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the
            # multi-turn fail_closed route must actually reach `blockers`
            # (the list `_apply_exit_code_mapping()` consumes), not merely
            # live inside known_context where the planner never reads it. ---
            _routed_scope_delta_decision = _kc["scope_delta_decision"]
            if (
                isinstance(_routed_scope_delta_decision, dict)
                and _routed_scope_delta_decision.get("status") == "fail_closed"
                and _routed_scope_delta_decision.get("reason")
                in ANCHOR_MULTI_TURN_FAIL_CLOSED_REASONS
            ):
                # PR #2171 fix_delta (P0-1, OWNER adversarial review):
                # membership in ANCHOR_MULTI_TURN_FAIL_CLOSED_REASONS (not
                # an exact match on a single reason string) is what makes
                # the integrity-unconfirmed forced-blocking route above
                # actually reach `blockers` / the final exit code.
                blockers.append(BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED)
                # PR #2171 fix_delta (P0-1, OWNER adversarial review): the
                # freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 built earlier
                # (before the multi-turn route could downgrade the
                # not_applicable pre-route decision to a blocking
                # fail_closed) must not survive into the final
                # known_context once the decision is confirmed blocking --
                # otherwise a fail-closed integrity gap would still leave
                # freeform authority evidence available to a downstream
                # consumer.
                _kc.pop("scope_delta_authority_evidence", None)

            known_context = _kc

    # --- Build raw snapshot (for artifact) ---
    raw_snapshot = {
        "schema_version": "raw_issue_snapshot/v1",
        "fetched_at": now or _now_iso(),
        "issue_number": issue_number,
        "repo": repo,
        "issue": issue,
        "comments": comments,
    }
    if anchor_comment_state is not None:
        raw_snapshot["anchor_comment"] = anchor_comment_state

    # --- Run repair pass before planner (Issue #889) ---
    # repair_issue_contract runs dry-run to report defects; the repaired body is
    # NOT fed to the planner (the planner always receives the original Issue body).
    # repair_result is included in the preflight output as repair_diagnostics (BLOCKER 1 fix).
    _repair_result = _invoke_repair(issue.get("body", "") or "")

    # PR #2202 review fix (P0-2): capture the repair_action.apply provenance
    # fields (source lane, run identity, live updatedAt, source refs digest)
    # at THIS producer run's own capture time, so a downstream
    # repair_action.apply consumer can bind against them instead of silently
    # seeing null/None (the schema already carries these under
    # `repair_action.*`; this producer previously never populated them).
    _repair_captured_at = now or _now_iso()
    _repair_original_updated_at = issue.get("updatedAt") if isinstance(issue, dict) else None
    _repair_source_lane = _determine_repair_source_lane(anchor_url_for_consumer, known_context)
    _repair_source_refs_digest_value = _repair_source_refs_digest(_repair_source_lane, anchor_url_for_consumer)
    _repair_preflight_run_identity_value = _repair_preflight_run_identity(
        issue_number=issue_number,
        repo=repo,
        original_body_sha256=_sha256(issue.get("body", "") or ""),
        captured_at=_repair_captured_at,
        source_lane=_repair_source_lane,
    )

    # --- Structural (template-derived) repair pass (Issue #995 fix_delta P0-1) ---
    # Connects build_structural_repair_bundle()/route_structural_repair_disposition()
    # to this production preflight execution path. Local-only resolution (see
    # _resolve_structural_repair_template()'s docstring); when the body has no
    # resolvable self-declared issue_kind, structural_repair_action stays None
    # and this run behaves exactly as before (field omitted -- schema-documented
    # as "absent when the producer was not invoked in structural-repair mode").
    structural_repair_action: Optional[dict[str, Any]] = None
    structural_repair_route: Optional[dict[str, Any]] = None
    if build_structural_repair_bundle is not None and route_structural_repair_disposition is not None:
        _struct_kind, _struct_template_text, _struct_template_relpath = _resolve_structural_repair_template(
            issue.get("body", "") or "", repo_root
        )
        if _struct_kind is not None:
            # Issue #995 fix_delta (P1-3, OWNER adversarial review): bind
            # template provenance to the git blob SHA of the actual template
            # FILE this bundle diffed against (not just a content-only SHA-256
            # of the text already carried in each item's template_digest),
            # plus the repo@commit ref this preflight run itself resolved.
            # repair_issue_contract.py stays pure Python (no git subprocess of
            # its own); the caller here supplies this optional provenance.
            _struct_git_blob_sha = _git_blob_sha(repo_root / _struct_template_relpath, repo_root)
            if _struct_git_blob_sha == "unknown":
                _struct_git_blob_sha = None
            _struct_head_sha = _git_head_sha(repo_root)
            _struct_source_ref = (
                f"{repo}@{_struct_head_sha}"
                if _struct_head_sha and _struct_head_sha != "unknown"
                else None
            )

            # Issue #2431: assemble known_scalars/source_spans from data
            # ALREADY available on this run (current Issue body's own MRC /
            # headings, plus -- only when `_repair_source_lane` (computed
            # above, reusing the SAME canonical human-context/anchor/
            # unanchored classifier `_determine_repair_source_lane()` --
            # no new trusted-anchor logic) is `human_context` -- the anchor
            # comment body) instead of leaving both unset, which previously
            # made every derivation-eligible missing section fall through to
            # human_review_required even when it was actually derivable
            # (#2426's manifestation).
            _struct_trusted_anchor_body = (
                anchor_body_for_consumer if _repair_source_lane == "human_context" else None
            )
            _struct_known_scalars: dict[str, Any] = {}
            _struct_parent_issue_scalar = _resolve_parent_issue_known_scalar(
                issue.get("body", "") or "",
                trusted_anchor_body=_struct_trusted_anchor_body,
            )
            if _struct_parent_issue_scalar is not None:
                for _struct_field_id in _PARENT_ISSUE_KNOWN_SCALAR_FIELD_IDS:
                    _struct_known_scalars[_struct_field_id] = _struct_parent_issue_scalar
            _struct_source_spans = _resolve_current_issue_heading_alias_source_spans(
                issue.get("body", "") or "",
                repo=repo,
                issue_number=issue_number,
            )
            _struct_owner_anchor_snapshot: Optional[dict[str, Any]] = None
            # Issue #2582: only the already-established explicit human-context
            # lane can reach the narrow owner-anchor assembler. It independently
            # binds the OWNER comment identity/snapshot/revision before it emits
            # any existing `owner_anchor` / `source_span_exact` provenance.
            if (
                _repair_source_lane == "human_context"
                and isinstance(anchor_body_for_consumer, str)
                and isinstance(anchor_url_for_consumer, str)
                and isinstance(anchor_payload_for_consumer, dict)
            ):
                _owner_anchor_spans = _resolve_owner_anchor_required_section_source_spans(
                    issue.get("body", "") or "",
                    anchor_body=anchor_body_for_consumer,
                    anchor_url=anchor_url_for_consumer,
                    anchor_comment=anchor_payload_for_consumer,
                    repo=repo,
                    issue_number=issue_number,
                )
                if _owner_anchor_spans:
                    _struct_owner_anchor_snapshot = {
                        "repo": repo,
                        "target_issue_number": issue_number,
                        "comment_id": str(anchor_payload_for_consumer["id"]),
                        "comment_url": anchor_url_for_consumer,
                        "body": anchor_body_for_consumer,
                        "body_sha256": "sha256:" + _sha256(anchor_body_for_consumer),
                        "comment_updated_at": anchor_payload_for_consumer.get("updated_at"),
                        "author_association": anchor_payload_for_consumer.get("author_association"),
                    }
                for _struct_field_id, _owner_span in _owner_anchor_spans.items():
                    _existing_span = _struct_source_spans.get(_struct_field_id)
                    if _existing_span is None:
                        _struct_source_spans[_struct_field_id] = _owner_span
                    elif isinstance(_existing_span, list):
                        _struct_source_spans[_struct_field_id] = [*_existing_span, _owner_span]
                    else:
                        # An independently-derived candidate for the same
                        # field is an ambiguity, not a priority decision.
                        _struct_source_spans[_struct_field_id] = [_existing_span, _owner_span]

            structural_repair_action = build_structural_repair_bundle(
                issue.get("body", "") or "",
                issue_kind=_struct_kind,
                template_text=_struct_template_text,
                template_path=_struct_template_relpath,
                repo=repo,
                issue_number=issue_number,
                original_updated_at=_repair_original_updated_at,
                known_scalars=_struct_known_scalars or None,
                source_spans=_struct_source_spans or None,
                owner_anchor_snapshot=_struct_owner_anchor_snapshot,
                template_git_blob_sha=_struct_git_blob_sha,
                template_source_ref=_struct_source_ref,
            )
            structural_repair_route = route_structural_repair_disposition(structural_repair_action)

    # Issue #2016 iteration-3 P1-3 (OWNER adversarial review): a repair

    # subprocess-level failure must dominate a *later* planner failure.
    # Established precedence (see _apply_exit_code_mapping docstring):
    #   environment_failure > blocked > needs_fix > warn > pass
    # Evaluated here -- before the planner is invoked at all -- so a
    # compound failure (repair subprocess broken AND planner exit 2) is
    # recorded as environment_failure via BLOCKER_REPAIR_ENVIRONMENT_FAILURE
    # (an env_blocker in _apply_exit_code_mapping) rather than surfacing
    # only as a planner-only blocked/human_judgment_required result that
    # silently drops the repair failure. The block below (after the planner
    # invocation) re-derives the same reason for the non-early-return path
    # and is a no-op here when this already fired.
    _repair_invocation_error_early = (
        _repair_result.get("error") if isinstance(_repair_result, dict) else "repair_result_not_object"
    )
    if _repair_invocation_error_early and BLOCKER_REPAIR_ENVIRONMENT_FAILURE not in blockers:
        blockers.append(BLOCKER_REPAIR_ENVIRONMENT_FAILURE)
        blockers.append(f"repair_invocation_error:{_repair_invocation_error_early}")

    # --- #1891 AC6: heavy mutation gate ---
    # Only evaluated when the caller (main thread / orchestrator) has stated
    # an intended mutation_category in known_context; this never fires for
    # ordinary body-improvement preflight runs that do not carry one.
    if known_context and known_context.get("mutation_category"):
        known_context = dict(known_context)
        # #2689: `_classify_heavy_mutation_gate_with_fresh_owner_reaction()`
        # issues the fresh `owner_reaction_decision.py` subprocess (ONLY for
        # mutation_category == "not_planned", ONLY when the caller supplied
        # `known_context["owner_reaction_context"]`) immediately before
        # delegating to `_classify_heavy_mutation_gate()` -- when that key is
        # absent (every pre-#2689 caller), behavior is byte-identical to the
        # prior direct `_classify_heavy_mutation_gate()` call this replaces.
        _heavy_mutation_gate = _classify_heavy_mutation_gate_with_fresh_owner_reaction(
            mutation_category=known_context.get("mutation_category"),
            scope_delta_decision=known_context.get("scope_delta_decision"),
            owner_reaction_context=known_context.get("owner_reaction_context"),
            repo=repo,
            issue_number=issue_number,
        )
        known_context["heavy_mutation_gate"] = _heavy_mutation_gate
        # #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the heavy
        # mutation gate must actually reach `blockers`, not merely live
        # inside known_context where the planner never reads it.
        if _heavy_mutation_gate.get("fail_closed") is True:
            blockers.append(BLOCKER_HEAVY_MUTATION_FAIL_CLOSED)

    # #2086 P0 fix_delta (iteration 3, Blocker 1): the ONLY real production
    # entrypoint through which `investigation_derived_path_literals` may
    # reach the planner is this validated, bound transport manifest -- see
    # `_validate_investigation_evidence_transport()`. A caller that hand-sets
    # `known_context["investigation_derived_path_literals"]` directly (the
    # pre-fix_delta test-only shortcut) is no longer what the CANONICAL CLI
    # -> `skill_runtime_exec.py` -> registry-rendered argv -> this subprocess
    # chain does; `main()` only ever threads this manifest path through, and
    # this is where it is validated and merged into known_context.
    #
    # Issue #2678 AC6: when a transport path is explicitly specified AND its
    # validation fails, this flag fail-closes the mutation consumer call
    # below (`consume_trusted_anchor_contract_patch_plan()`) even when an
    # anchor-body-only patch plan is independently derivable via one of the
    # other producer branches (noop-satisfied fallback / structured scope
    # reframe synthesis) further down. Appending to `blockers` alone (as
    # before this Issue) does not stop those other branches from still
    # reaching the mutation consumer -- only this flag does.
    _investigation_evidence_transport_rejected = False
    if investigation_evidence_transport_path is not None:
        # Issue #2199 OWNER feedback P1-3: `investigation_evidence_transport_path`
        # is validated by the executor (skill_runtime_command_policy.py) as a
        # path relative to the PRIMARY checkout, but this script's own
        # `repo_root` (above) is `_find_repo_root()`, derived from THIS
        # script's own `__file__` -- the #2197/#2199 dedicated worktree when
        # dispatched there, not the primary checkout the caller validated
        # against. `investigation_evidence_primary_root` is the executor's
        # explicit handoff of that origin (set ONLY for the 4 production
        # preflight profiles); falling back to `repo_root` when absent keeps
        # every other caller (fixture profiles, direct Python callers, this
        # script's own pre-existing test suite) byte-identical to before this
        # parameter existed.
        _investigation_evidence_confinement_root = investigation_evidence_primary_root or repo_root
        # `_confine_artifact_path()` (inside `_validate_investigation_evidence_transport`)
        # resolves a RELATIVE `path` via `Path.resolve()`, which anchors to
        # this PROCESS's actual `os.getcwd()` -- NOT to whatever `repo_root`
        # value is passed alongside it. This process's cwd is always the
        # #2197/#2199 dedicated worktree once dispatched there (set by the
        # executor's `subprocess.run(cwd=execution_root, ...)`), regardless
        # of `investigation_evidence_primary_root`. Pre-joining the relative
        # transport path against `_investigation_evidence_confinement_root`
        # here -- making it ABSOLUTE before it ever reaches
        # `_confine_artifact_path()` -- makes this resolution independent of
        # this process's actual cwd, instead of merely widening the
        # (cwd-anchored) boundary check while leaving the resolved candidate
        # path itself still cwd-anchored.
        _investigation_evidence_transport_path_for_confinement = investigation_evidence_transport_path
        if not Path(_investigation_evidence_transport_path_for_confinement).is_absolute():
            _investigation_evidence_transport_path_for_confinement = (
                _investigation_evidence_confinement_root / _investigation_evidence_transport_path_for_confinement
            )
        _validated_literals, _transport_reason = _validate_investigation_evidence_transport(
            _investigation_evidence_transport_path_for_confinement,
            repo_root=_investigation_evidence_confinement_root,
            issue_number=issue_number,
            repo=repo,
            anchor_url=anchor_url_for_consumer,
            base_issue_body_sha256=_sha256(issue.get("body", "") or ""),
            git_head_sha=_git_head_sha(_investigation_evidence_confinement_root),
        )
        if _validated_literals is not None:
            known_context = dict(known_context) if known_context else {}
            known_context["investigation_derived_path_literals"] = _validated_literals
        else:
            blockers.append(f"investigation_evidence_transport_rejected:{_transport_reason}")
            _investigation_evidence_transport_rejected = True

    # --- Invoke planner ---
    known_context = _ensure_scope_signal_delta_input(
        repo_root=repo_root,
        issue=issue,
        raw_snapshot=raw_snapshot,
        known_context=known_context,
        issue_number=issue_number,
        repo=repo,
    )
    # Issue #2102 fix_delta (iteration 4, Blocker 4): populate
    # known_context["main_drift"] from a live git readback (see
    # `build_live_main_drift_known_context()` above) so
    # `plan_refinement_loop.py`'s `_refinement_main_drift_decision()` is
    # actually reachable in production, not just from hand-injected test
    # `known_context`. Gated behind `enable_main_drift_live_readback`
    # (default False, opt-in) rather than unconditional in every live-mode
    # call: many existing callers configure a real `origin` remote URL in
    # environments with no network access to it (or, worse, one that IS
    # reachable but unrelated to the local repo state under test), and an
    # unconditional live `git fetch` there would be a silent, surprising
    # side effect on every ordinary preflight invocation. Fixture-mode
    # invocations (`fixture_path is not None`) never run this regardless
    # of the flag; an explicitly-supplied `known_context["main_drift"]`
    # (fixture or caller override) is never overwritten.
    if (
        enable_main_drift_live_readback
        and fixture_path is None
        and _main_drift_classifier_probe is not None
        and not (known_context and "main_drift" in known_context)
    ):
        _live_main_drift = build_live_main_drift_known_context(
            repo_root=repo_root,
            issue_body=issue.get("body", "") or "",
        )
        if _live_main_drift is not None:
            known_context = dict(known_context) if known_context else {}
            known_context["main_drift"] = _live_main_drift
    planner_input_dict = _build_planner_input(
        issue,
        comments,
        known_context,
        anchor_comment_feedback=anchor_comment_feedback,
        anchor_comment_ids=anchor_comment_ids,
        now=now,
    )
    # #1677 AC4: join a previously-persisted scope-rollup artifact (if any)
    # into the planner input so ISSUE_EXECUTION_DECISION_V1 reflects known
    # collisions instead of always defaulting to 'selected'.
    _scope_rollup_plan = _load_scope_rollup_artifact(repo_root, issue_number)
    planner_input_dict = _join_scope_rollup_into_planner_input(planner_input_dict, _scope_rollup_plan)
    plan, planner_exit_code, planner_stderr, planner_stdout_raw = _invoke_planner(
        planner_input_dict, repo_root=repo_root, issue_number=issue_number
    )

    if plan is None:
        # Planner invocation failed
        if planner_exit_code == 2:
            blockers.append(BLOCKER_PLANNER_INVALID_INPUT)
        else:
            blockers.append(BLOCKER_PLANNER_INTERNAL_ERROR)

        # --- Blocker 3: failure classification sidecar ---
        failure_cls = classify_planner_failure(
            exit_code=planner_exit_code,
            stdout=planner_stdout_raw,
            stderr=planner_stderr,
            script_path=PLANNER_SCRIPT,
            python_executable=sys.executable,
        )

        try:
            _cls_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
            _cls_dir.mkdir(parents=True, exist_ok=True)
            (_cls_dir / "planner_failure_classification_v1.json").write_text(
                json.dumps(failure_cls, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
            )
        except Exception:
            pass

        # --- Blocker 1 (failure path): provenance sidecar ---
        try:
            _anchor_url = anchor_comment_urls[0] if anchor_comment_urls else ""
            _status_str_prov, _ = _apply_exit_code_mapping(planner_exit_code, None, blockers)
            _prov = build_provenance(
                repo=repo,
                issue_number=issue_number,
                anchor_comment_url=_anchor_url,
                planner_input=planner_input_dict,
                raw_snapshot=raw_snapshot,
                wrapper_exit_code=EXIT_ENVIRONMENT_FAILURE,
                wrapper_status=_status_str_prov,
                blockers=blockers,
                stderr=planner_stderr or "",
                repo_root=repo_root,
                plan=None,
                result_next_action="fix_environment",
            )
            write_provenance_artifact(repo_root, issue_number, _prov)
        except Exception:
            pass

        status_str, _ = _apply_exit_code_mapping(planner_exit_code, None, blockers)
        if planner_exit_code == 3:
            status_str = "environment_failure"
        return _emit_failure_result(
            repo_root=repo_root,
            issue_number=issue_number,
            repo=repo,
            status=status_str,
            next_action="fix_environment" if status_str == "environment_failure" else "human_judgment_required",
            blockers=blockers,
            planner_fail_closed_reason_codes=[BLOCKER_PLANNER_INTERNAL_ERROR],
            required_sections=[],
            required_contract_keys=[],
            rewrite_constraints=_build_safe_rewrite_constraints([], []),
            planner_exit_code=planner_exit_code,
            planner_fail_closed=True,
            planner_input=planner_input_dict,
            raw_snapshot=raw_snapshot,
        )

    # The planner subprocess cannot consume the raw trusted comment. Rebind
    # its existing sidecar to the fetched, hash-checked source before mutation
    # or result publication; this prevents H2 conflicts becoming partial plans.
    if isinstance(known_context, dict) and isinstance(anchor_body_for_consumer, str) and anchor_url_for_consumer:
        _bind_trusted_scope_patch_plan_to_source(
            plan, known_context, anchor_body=anchor_body_for_consumer,
            anchor_url=anchor_url_for_consumer, repo=repo, issue_number=issue_number,
            issue_body_sha256=_sha256(issue.get("body", "")),
        )

    # A close-only disposition never proposes or consumes a scope-delta update.
    _close_not_planned_disposition = _is_approved_close_not_planned_decision(
        known_context.get("scope_delta_decision") if isinstance(known_context, dict) else None
    )
    # Issue #2678 AC6: an explicitly-specified-and-rejected transport path
    # fail-closes this entire mutation-consumer branch -- the mutation
    # callback (`consume_trusted_anchor_contract_patch_plan()`) and any
    # GitHub update request it could trigger are unreachable, regardless of
    # whether an anchor-body-only patch plan is independently derivable
    # below (noop-satisfied fallback / structured scope reframe synthesis).
    if (
        consume_contract_patch_plan
        and not _close_not_planned_disposition
        and not _investigation_evidence_transport_rejected
    ):
        sidecar = plan.get("scope_signal_guard_decision_v2")
        authority = sidecar.get("scope_delta_authority") if isinstance(sidecar, dict) else None
        patch_plan = authority.get("contract_patch_plan") if isinstance(authority, dict) else None
        # PR #2057 OWNER review (iteration 4, blocker 3 / P0-1 residual):
        # `authority` (the freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1
        # producer's `classify_scope_delta_authority()` result) is `None`
        # in two distinct cases that must NOT be conflated: (a) the
        # freeform evidence builder (`_build_scope_delta_authority_
        # evidence()`, invoked earlier in this same `run_preflight()` call
        # for this same anchor comment) itself failed -- structural anchor
        # URL mismatch or a `scope_signal_delta` parser/import failure --
        # even though the STRUCTURED `ANCHOR_SCOPE_REFRAME_V1` parser
        # succeeded (this branch only runs once that structured parser has
        # already validated a non-empty `allowed_path_deltas`); or (b) the
        # evidence builder succeeded and `classify_scope_delta_authority()`
        # ran to completion but reached an ordinary business decision that
        # is not `contract_update_required` (e.g. `human_escalation` for a
        # purely-structured, non-freeform-directive anchor comment -- the
        # common, legitimate case this branch was originally written for).
        # `known_context["scope_delta_authority_evidence"]` is only set
        # when the evidence builder succeeded (see the `_kc[...] = [...]`
        # assignment above in this function), so its presence distinguishes
        # (a) -- genuine "patch-plan producer unavailable", fail closed to
        # `invalid` -- from (b), left untouched (`True`).
        _patch_plan_producer_available = (
            isinstance(known_context, dict)
            and "scope_delta_authority_evidence" in known_context
        )
        if (
            not isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
        ):
            patch_plan = _satisfied_trusted_directive_noop_patch_plan(
                plan=plan,
                issue=issue,
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url_for_consumer,
                anchor_payload=anchor_payload_for_consumer,
                anchor_body=anchor_body_for_consumer,
                known_context=known_context,
            )
        if (
            not isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
            and _extract_validated_scope_delta_deltas(
                known_context=known_context,
                anchor_url=anchor_url_for_consumer,
                anchor_body=anchor_body_for_consumer,
            )
        ):
            # #2048 Scope Delta: an approved trusted-anchor scope reframe
            # (structured ANCHOR_SCOPE_REFRAME_V1, non-empty
            # allowed_path_deltas) has no operations[] producer of its own --
            # the freeform scope_delta_authority classifier only emits a
            # contract_patch_plan when it can extract an exact "allowed
            # paths" literal (an ambiguous/no-literal directive fails closed
            # to human_escalation instead), and the noop-satisfied fallback
            # above requires a non-empty, already-applied operations[].
            # Without this synthesis an approved scope reframe whose deltas
            # cannot be expressed as a section-bound append would never reach
            # the consumer at all (patch_plan stays None and the mutation
            # phase falls into the unconditional "no safe action" failure
            # branch below) -- the exact #2048 PR review regression this
            # Issue's Scope Delta targets. Synthesize a schema-valid,
            # EMPTY-operations CONTRACT_PATCH_PLAN_V1 (never a new field, an
            # ordinary instance of the existing schema) only when the
            # validated decision is bound to THIS anchor/body -- the
            # consumer boundary re-validates the same binding independently.
            #
            # PR #2057 OWNER review P0-1 (fully resolved via
            # `patch_plan_producer_available`, iteration 4 blocker 3 -- see
            # `consume_trusted_anchor_contract_patch_plan()` above and
            # `test_producer_unavailable_reaches_invalid_disposition_via_
            # production_consumer` / `test_producer_available_default_true_
            # preserves_full_rewrite_required` in
            # `test_preflight_run_with_anchor.py`): this synthesis is
            # additionally gated on `_classify_scope_delta_decision()`
            # returning `"valid"`, which requires the STRUCTURED
            # `ANCHOR_SCOPE_REFRAME_V1` payload (`_classify_anchor_scope_
            # reframe()`, a schema-validating, anchor-URL/hash-bound parser
            # distinct from the FREEFORM `SCOPE_DELTA_AUTHORITY_EVIDENCE_V1`
            # classifier that separately produces `contract_patch_plan` /
            # `human_escalation`) to have explicitly parsed and validated
            # `status: approve_scope_delta`. `operations: []` here reflects
            # that the structured reframe payload carries no operations
            # concept by construction (it conveys `allowed_path_deltas`
            # only) -- it is NOT synthesized merely because "no patch plan
            # exists"; a `contract_patch_plan_operations_not_list` /
            # `invalid_operations_key_missing` classification is never
            # reachable through this branch specifically because
            # `operations` is always populated here.
            from scope_signal_delta import build_contract_patch_plan_v1

            patch_plan = build_contract_patch_plan_v1(
                target_issue_number=issue_number,
                base_issue_body_sha256=_sha256(issue.get("body", "")),
                source_evidence=[],
                operations=[],
            )
        if (
            anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
        ):
            consumer_result = consume_trusted_anchor_contract_patch_plan(
                repo=repo,
                issue_number=issue_number,
                issue=issue,
                anchor_url=anchor_url_for_consumer,
                anchor_payload=anchor_payload_for_consumer,
                anchor_body=anchor_body_for_consumer,
                contract_patch_plan=patch_plan if isinstance(patch_plan, dict) else None,
                callbacks=contract_update_callbacks,
                known_context=known_context,
                patch_plan_producer_available=_patch_plan_producer_available,
            )
            contract_update_handoff = _bounded_contract_update_handoff(consumer_result)
            _rewrite_route = (
                consumer_result.get("rewrite_route") if isinstance(consumer_result, dict) else None
            )
            contract_update_route_issue_editor_required = (
                isinstance(_rewrite_route, dict) and _rewrite_route.get("route") == "issue_editor_required"
            )
            if contract_update_route_issue_editor_required:
                # PR #2057 OWNER review P0-2/P1-3: a full_rewrite_required
                # disposition is a legitimate routing outcome, not a failed
                # mutation attempt -- no write was ever attempted
                # (operations[] was empty). `_bounded_contract_update_
                # handoff()` already projects this as the distinct
                # `status: handoff_required` (never `failed`, never
                # `no_change`); this branch intentionally does NOT overwrite
                # it and does NOT append BLOCKER_FAIL_CLOSED -- the dedicated
                # next_action override below carries the signal instead.
                pass
            elif contract_update_handoff.get("status") not in {"applied", "no_change", "rebased"}:
                blockers.append(BLOCKER_CONTRACT_UPDATE_FAILED)
        else:
            # The explicit mutation phase has no safe action without a
            # planner-produced and provenance-bound patch plan.
            contract_update_handoff = {
                "status": "failed",
                "writes": 0,
                "iterations": 0,
                "final_readback": "failed",
                "fresh_preflight": "unavailable",
                "fresh_review": "unavailable",
                "fresh_readiness": "unavailable",
                "reason_code": "contract_patch_plan_missing",
            }
            blockers.append(BLOCKER_CONTRACT_UPDATE_FAILED)

    # --- Extract planner output fields ---
    fail_closed = plan.get("fail_closed", {})
    planner_fail_closed = fail_closed.get("required", False)

    # Build must_read / do_not_read from planner decisions
    must_read: list[str] = []
    do_not_read: list[str] = []

    decisions = plan.get("decisions", {})
    investigation_policy = decisions.get("investigation_policy", {})
    if investigation_policy.get("required"):
        target_paths = investigation_policy.get("target_paths", [])
        must_read.extend(target_paths)

    # Build commands
    commands = _commands_from_plan(plan, issue_number, repo)

    # Planner blockers
    # #1677 AC12 (PR #1767 owner review, P0-4): actually invoke the shared
    # semantic validator here instead of importing it unused. A missing
    # validator import or a semantically invalid issue_execution_decision
    # blocks the preflight rather than silently passing plan.
    _issue_execution_decision = plan.get("issue_execution_decision")
    if isinstance(_issue_execution_decision, dict):
        if validate_issue_execution_decision is None:
            blockers.append(BLOCKER_ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE)
        else:
            _ied_violations = validate_issue_execution_decision(_issue_execution_decision)
            if _ied_violations:
                blockers.append(f"{BLOCKER_ISSUE_EXECUTION_DECISION_INVALID}: " + ", ".join(_ied_violations))

    if planner_exit_code == 2:
        blockers.append(BLOCKER_PLANNER_INVALID_INPUT)
    elif planner_exit_code == 3:
        blockers.append(BLOCKER_PLANNER_INTERNAL_ERROR)
    elif planner_exit_code == 0 and planner_fail_closed:
        blockers.append(BLOCKER_FAIL_CLOSED)
        reason_codes, reason_codes_ok = _as_string_list(
            fail_closed.get("reason_codes", []),
            "planner.fail_closed.reason_codes",
            blockers,
        )
        planner_fail_closed_reason_codes = reason_codes
        blockers.extend(reason_codes)
        if not reason_codes_ok:
            rewrite_constraints = _build_safe_rewrite_constraints([], [])

        rc = fail_closed.get("rewrite_constraints", {})
        if not isinstance(rc, dict):
            blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: rewrite_constraints must be an object")
            rewrite_constraints = _build_safe_rewrite_constraints([], [])
            reason_codes_ok = False
        elif not rc.get("schema_version"):
            rewrite_constraints = _build_safe_rewrite_constraints([], [])
        elif reason_codes_ok:
            if not _ensure_json_serializable(rc, "planner.fail_closed.rewrite_constraints", blockers):
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                reason_codes_ok = False
            else:
                required_sections, sections_ok = _as_string_list(
                    rc.get("required_sections", []),
                    "planner.fail_closed.rewrite_constraints.required_sections",
                    blockers,
                )
                required_contract_keys, keys_ok = _as_string_list(
                    rc.get("required_contract_keys", []),
                    "planner.fail_closed.rewrite_constraints.required_contract_keys",
                    blockers,
                )
                if sections_ok and keys_ok:
                    rewrite_constraints = rc
                else:
                    rewrite_constraints = _build_safe_rewrite_constraints([], [])
                    reason_codes_ok = False

        # AC7: Invariant check — required_sections/required_contract_keys must match
        # the nested must_add_sections/must_add_contract_keys in rewrite_constraints.
        if rewrite_constraints is not None and reason_codes_ok:
            rc_inner = rewrite_constraints.get("rewrite_constraints", {})
            must_add_sections = rc_inner.get("must_add_sections", [])
            must_add_keys = rc_inner.get("must_add_contract_keys", [])
            if list(required_sections) != list(must_add_sections):
                blockers.append(
                    f"{BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION}: "
                    f"required_sections {required_sections!r} != "
                    f"must_add_sections {must_add_sections!r}"
                )
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                required_sections = []
                required_contract_keys = []
                reason_codes_ok = False
            elif list(required_contract_keys) != list(must_add_keys):
                blockers.append(
                    f"{BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION}: "
                    f"required_contract_keys {required_contract_keys!r} != "
                    f"must_add_contract_keys {must_add_keys!r}"
                )
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                required_sections = []
                required_contract_keys = []
                reason_codes_ok = False

        if not reason_codes_ok:
            # Schema-safe deterministic forwarding requires aligned payloads.
            # Non-string / non-list fields are treated as schema violation.
            blockers.append(BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID)

    # --- Write repair artifact and route repair_action.disposition (Issue #2016) ---
    # BLOCKER 1 fix (original): repair_diagnostics is exposed via artifact file (not as a
    # top-level result key, which would violate schema additionalProperties: false).
    #
    # Issue #2016 (OWNER adversarial review P0-2/P0-3): "blocker not added" alone is not
    # enough to signal needs_fix (it would silently become pass/proceed), and the repair
    # subprocess / artifact write path must be fail-closed rather than fail-open. This
    # block therefore branches into exactly one of three outcomes:
    #   1. repair_environment_failure_reason set -> BLOCKER_REPAIR_ENVIRONMENT_FAILURE
    #      (routes to status=environment_failure via env_blockers in
    #      _apply_exit_code_mapping()).
    #   2. repair_needs_fix=True -> no blocker added; repair_action_projection built for
    #      the canonical result + compact stdout (routes to status=needs_fix below).
    #   3. otherwise (human_review_required / informational-but-changed) -> existing
    #      generic repair_diagnostics blocker (routes to status=blocked, unchanged
    #      behavior from before Issue #2016).
    repair_artifact_path: Optional[str] = None
    repair_action_projection: Optional[dict[str, Any]] = None
    repair_needs_fix = False
    repair_environment_failure_reason: Optional[str] = None

    _repair_error = _repair_result.get("error") if isinstance(_repair_result, dict) else "repair_result_not_object"
    _repair_action_raw = _repair_result.get("repair_action") if isinstance(_repair_result, dict) else None
    _repair_changed = _repair_result.get("changed") if isinstance(_repair_result, dict) else None

    artifact_dir_repair = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    try:
        artifact_dir_repair.mkdir(parents=True, exist_ok=True)
        repair_artifact_file = artifact_dir_repair / "repair_diagnostics.json"
        _atomic_write_json(repair_artifact_file, _repair_result)
        # Readback validation (P0-3): only trust the artifact path once the
        # bytes round-trip, matching the existing _atomic_write_json contract
        # used elsewhere (raw_issue_snapshot / planner_input / result).
        _repair_readback = json.loads(repair_artifact_file.read_text(encoding="utf-8"))
        if _repair_readback != _repair_result:
            repair_environment_failure_reason = "repair_diagnostics_artifact_readback_mismatch"
        else:
            repair_artifact_path = str(repair_artifact_file)
    except Exception as exc:
        repair_environment_failure_reason = f"repair_diagnostics_artifact_write_failed:{type(exc).__name__}"

    if repair_environment_failure_reason is None and _repair_error:
        repair_environment_failure_reason = f"repair_invocation_error:{_repair_error}"

    if (
        repair_environment_failure_reason is None
        and isinstance(_repair_result, dict)
        and _repair_result.get("schema") != "repair_issue_contract/v1"
    ):
        repair_environment_failure_reason = "repair_schema_mismatch"

    if repair_environment_failure_reason is None and _repair_changed is True:
        # changed: true with 0 mutating repairs described is a producer/wrapper
        # contract inconsistency, not a legitimate no-repair state.
        if not isinstance(_repair_action_raw, dict) or not _repair_action_raw.get("repair_kinds"):
            repair_environment_failure_reason = "repair_changed_with_no_mutating_repair"

    if (
        repair_environment_failure_reason is None
        and isinstance(_repair_action_raw, dict)
        and _repair_action_raw.get("disposition") == "invalid_payload"
    ):
        repair_environment_failure_reason = "repair_action_invalid_payload"

    if repair_environment_failure_reason is not None:
        if BLOCKER_REPAIR_ENVIRONMENT_FAILURE not in blockers:
            blockers.append(BLOCKER_REPAIR_ENVIRONMENT_FAILURE)
    elif (
        _repair_changed is True
        and isinstance(_repair_action_raw, dict)
        and _repair_action_raw.get("disposition") == "auto_apply_safe"
        and repair_artifact_path is not None
    ):
        _apply_result, _apply_error = _materialize_auto_apply_candidate(
            issue.get("body", "") or "", _repair_result, artifact_dir_repair
        )
        if _apply_error is not None:
            blockers.append(BLOCKER_REPAIR_ENVIRONMENT_FAILURE)
            repair_environment_failure_reason = _apply_error
        else:
            _candidate_body_artifact = str(artifact_dir_repair / "repaired_issue_body.md")
            repair_action_projection = {
                "schema_version": _repair_action_raw.get("schema_version", "repair_action/v1"),
                "policy_version": _repair_action_raw.get("policy_version", "deterministic-issue-repair/v1"),
                "disposition": "auto_apply_safe",
                "original_body_sha256": _repair_action_raw.get("original_body_sha256"),
                "repaired_body_sha256": _repair_action_raw.get("repaired_body_sha256"),
                "diagnostics_artifact": repair_artifact_path,
                "candidate_body_artifact": _candidate_body_artifact,
                "repair_kinds": _repair_action_raw.get("repair_kinds", []),
                "reason_codes": _repair_action_raw.get("reason_codes", []),
                # PR #2202 review fix (P0-2): AC9 provenance-lane fields
                # (previously always absent, silently defeating
                # repair_action.apply's provenance binding).
                "source_lane": _repair_source_lane,
                "preflight_run_identity": _repair_preflight_run_identity_value,
                "original_updated_at": _repair_original_updated_at,
                "source_refs_digest": _repair_source_refs_digest_value,
            }
            repair_needs_fix = True
    elif _repair_changed is True and repair_artifact_path is not None:
        # human_review_required / informational-but-changed / unknown disposition:
        # existing generic blocker behavior (unchanged from before Issue #2016).
        blockers.append(
            json.dumps(
                {
                    "kind": "repair_diagnostics",
                    "message": "repair_issue_contract detected changes: see repair artifact for details",
                    "artifact_path": repair_artifact_path,
                }
            )
        )

    # --- Apply exit code mapping (with plan for warn detection, after all blockers finalized) ---
    _scope_delta_decision_for_exit_mapping = (
        known_context.get("scope_delta_decision") if isinstance(known_context, dict) else None
    )
    status, exit_code = _apply_exit_code_mapping(
        planner_exit_code,
        planner_fail_closed,
        blockers,
        plan=plan,
        scope_delta_decision=_scope_delta_decision_for_exit_mapping,
        repair_needs_fix=repair_needs_fix,
    )

    # A verified OWNER close-only decision is a termination disposition, not
    # a malformed implementation contract. Remove only the missing-contract
    # fail-closed result and retain every unrelated blocker fail-closed.
    _close_only_missing_contract_blockers = all(
        blocker == BLOCKER_FAIL_CLOSED or blocker.startswith("missing_required_section")
        for blocker in blockers
    )
    if _close_not_planned_disposition and _close_only_missing_contract_blockers:
        blockers[:] = []
        planner_fail_closed_reason_codes = []
        planner_fail_closed = False
        status = "warn"
        exit_code = EXIT_WARN

    # Determine next_action
    if _close_not_planned_disposition and _close_only_missing_contract_blockers:
        next_action = "human_judgment_required"
    elif status == "pass":
        next_action = "proceed"
    elif status == "needs_fix":
        next_action = "apply_deterministic_repair"
    elif status == "warn":
        next_action = "proceed_with_notes"
    elif status == "blocked":
        next_action = "human_judgment_required"
    else:
        next_action = "fix_environment"

    # --- Structural repair routing (Issue #995 fix_delta P0-1) ---
    # `refinement_preflight_result_v1.schema.json`'s allOf invariants require
    # `status` to be the EXACT const the attached structural_repair_action's
    # disposition_summary dictates (auto_apply_safe -> needs_fix,
    # human_review_required -> blocked). Honor the SAME
    # environment_failure > blocked > needs_fix > warn > pass precedence
    # documented above for the sibling repair_action lane: only apply the
    # structural verdict when it would not force a downgrade of a
    # MORE severe pre-existing status. When it would, the bundle is left
    # unattached this run (schema-safe) and its reason codes are still
    # surfaced as non-status-affecting informational blockers so the
    # finding is never silently dropped.
    if (
        structural_repair_action is not None
        and structural_repair_route is not None
        and not _close_not_planned_disposition
    ):
        _structural_target_status = structural_repair_route["status"]
        if _structural_target_status == STRUCTURAL_REPAIR_ROUTE_STATUS_PASS:
            # no_missing_fields_detected: no status constraint; always safe.
            pass
        else:
            _status_severity = {
                "pass": 0,
                "warn": 1,
                "needs_fix": 2,
                "blocked": 3,
                "environment_failure": 4,
            }
            _current_rank = _status_severity.get(status, 0)
            _target_rank = _status_severity[_structural_target_status]
            if _current_rank > _target_rank and (
                _structural_target_status == STRUCTURAL_REPAIR_ROUTE_STATUS_NEEDS_FIX
                and _structural_deadlock_override_eligible(
                    structural_repair_action, blockers, required_sections, required_contract_keys
                )
            ):
                # Issue #2396: the incoming structural candidate would
                # itself RESOLVE the same missing_required_section blocker
                # that made the pre-existing status more severe than the
                # structural target -- adopting the (weaker-ranked)
                # structural verdict here does not silently downgrade an
                # UNRELATED, more severe finding; it is a fix for the exact
                # blocker(s) present. Deferring in this specific case would
                # permanently deadlock a fixable Issue (the #2180 incident).
                # Every covered missing_required_section /
                # structural_repair_action_deferred blocker is cleared
                # (resolved by the adopted action), never left stale
                # alongside a needs_fix status.
                status = _structural_target_status
                next_action = "apply_deterministic_structural_repair"
                exit_code = EXIT_NEEDS_FIX
                blockers[:] = [
                    _b
                    for _b in blockers
                    if not _structural_deadlock_eligible_blocker(_b, structural_repair_action)
                ]
            elif _current_rank > _target_rank:
                for _struct_rc in structural_repair_route["reason_codes"]:
                    _struct_entry = f"structural_repair_action_deferred:{_struct_rc}"
                    if _struct_entry not in blockers:
                        blockers.append(_struct_entry)
                structural_repair_action = None
                structural_repair_route = None
            else:
                status = _structural_target_status
                if _structural_target_status == STRUCTURAL_REPAIR_ROUTE_STATUS_NEEDS_FIX:
                    next_action = "apply_deterministic_structural_repair"
                    exit_code = EXIT_NEEDS_FIX
                else:
                    next_action = "human_judgment_required"
                    exit_code = EXIT_BLOCKED
                    for _struct_rc in structural_repair_route["reason_codes"]:
                        _struct_entry = f"structural_repair_action:{_struct_rc}"
                        if _struct_entry not in blockers:
                            blockers.append(_struct_entry)


    # #2048 Scope Delta: an approved trusted-anchor scope reframe whose

    # operations[] is empty AND is not yet reflected in the current body
    # (full_rewrite_required) is routed to issue_editor_required regardless
    # of the ordinary status-derived next_action above. This is the ONLY
    # code path that projects `consume_trusted_anchor_contract_patch_plan()`'s
    # `rewrite_route` all the way to the wrapper's stdout / result artifact
    # `next_action` -- the previous #2048 iterations computed
    # `decide_scope_reframe_contract_route()` but never reached this
    # projection because the classification was unreachable from this
    # consumer boundary (see `_extract_validated_scope_delta_deltas()`).
    if contract_update_route_issue_editor_required:
        next_action = "issue_editor_required"

    # --- Compute hashes for byte-stability (after all blockers finalized) ---
    snapshot_text = json.dumps(raw_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
    planner_input_text = json.dumps(planner_input_dict, sort_keys=True, ensure_ascii=False, allow_nan=False)

    # Core result (without artifacts/hashes) for hash computation
    result_core_for_hash = {
        "schema_version": SCHEMA_VERSION_RESULT,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "planner_exit_code": planner_exit_code,
        "planner_fail_closed": planner_fail_closed,
        "next_action": next_action,
        "must_read": sorted(set(must_read)),
        "do_not_read": do_not_read,
        "commands": commands,
        "blockers": blockers,
        "planner_fail_closed_reason_codes": planner_fail_closed_reason_codes,
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "rewrite_constraints": rewrite_constraints,
    }
    if contract_update_handoff is not None:
        result_core_for_hash["contract_update"] = contract_update_handoff
    # Issue #2016 iteration-3 P1-2 (OWNER adversarial review): result_core_sha256
    # previously excluded repair_action.schema_version/.policy_version/.disposition/
    # body SHAs/repair_kinds/reason_codes, so those machine-actionable fields could
    # be silently altered without changing the result's integrity hash. Bind a
    # stable, environment-independent projection of them here (explicitly
    # excluding the absolute diagnostics_artifact/candidate_body_artifact paths,
    # which are environment-dependent).
    if isinstance(repair_action_projection, dict):
        result_core_for_hash["repair_action_core"] = {
            "schema_version": repair_action_projection.get("schema_version"),
            "policy_version": repair_action_projection.get("policy_version"),
            "disposition": repair_action_projection.get("disposition"),
            "original_body_sha256": repair_action_projection.get("original_body_sha256"),
            "repaired_body_sha256": repair_action_projection.get("repaired_body_sha256"),
            "repair_kinds": sorted(repair_action_projection.get("repair_kinds", []) or []),
            "reason_codes": sorted(repair_action_projection.get("reason_codes", []) or []),
        }
    # Issue #995 fix_delta (P0-1): mirror the same integrity-hash binding for
    # structural_repair_action -- otherwise its disposition/items could be
    # silently altered without changing result_core_sha256, exactly the
    # P1-2 gap the repair_action_core binding above was added to close.
    if isinstance(structural_repair_action, dict):
        result_core_for_hash["structural_repair_action_core"] = {
            "schema_version": structural_repair_action.get("schema_version"),
            "policy_version": structural_repair_action.get("policy_version"),
            "issue_kind": structural_repair_action.get("issue_kind"),
            "disposition_summary": structural_repair_action.get("disposition_summary"),
            "original_body_sha256": structural_repair_action.get("original_body_sha256"),
            "item_field_ids": sorted(
                i.get("field_id") for i in structural_repair_action.get("items", []) if isinstance(i, dict)
            ),
        }
    result_core_text = json.dumps(result_core_for_hash, sort_keys=True, ensure_ascii=False, allow_nan=False)


    hashes = {
        "raw_issue_snapshot_sha256": _sha256(snapshot_text),
        "planner_input_sha256": _sha256(planner_input_text),
        "result_core_sha256": _sha256(result_core_text),
    }

    artifact_dir = _issue_artifact_dir(repo_root, issue_number)
    artifacts = {
        "raw_issue_snapshot": str(artifact_dir / "raw_issue_snapshot.json"),
        "planner_input": str(artifact_dir / "planner_input.json"),
        "refinement_preflight_result_v1": str(artifact_dir / "refinement_preflight_result_v1.json"),
    }
    # Issue #2016 iteration-3 P1-1: AC6 requires repair diagnostics and the
    # candidate body to be referenceable from ALL THREE of the result
    # schema, the artifact map, and compact stdout. repair_action already
    # carries diagnostics_artifact/candidate_body_artifact; this mirrors
    # those same paths into the canonical artifacts map so consumers that
    # only read `artifacts` (not the nested repair_action block) can still
    # discover them.
    if status == "needs_fix" and isinstance(repair_action_projection, dict):
        if repair_action_projection.get("diagnostics_artifact"):
            artifacts["repair_diagnostics"] = repair_action_projection["diagnostics_artifact"]
        if repair_action_projection.get("candidate_body_artifact"):
            artifacts["repair_candidate_body"] = repair_action_projection["candidate_body_artifact"]
    result = _build_result(
        status=status,
        issue_number=issue_number,
        repo=repo,
        planner_exit_code=planner_exit_code,
        planner_fail_closed=planner_fail_closed,
        next_action=next_action,
        must_read=sorted(set(must_read)),
        do_not_read=do_not_read,
        commands=commands,
        blockers=blockers,
        planner_fail_closed_reason_codes=planner_fail_closed_reason_codes,
        required_sections=required_sections,
        required_contract_keys=required_contract_keys,
        rewrite_constraints=rewrite_constraints,
        artifacts=artifacts,
        hashes=hashes,
        contract_update=contract_update_handoff,
        repair_action=repair_action_projection,
        structural_repair_action=structural_repair_action,
        environment_failure_reason_code=(
            _classify_final_environment_failure(blockers, planner_exit_code)
            if status == "environment_failure"
            else None
        ),
        environment_failure_source="internal" if status == "environment_failure" else None,
        environment_failure_operation="planner_invocation" if status == "environment_failure" else None,
    )
    try:
        _write_artifacts(repo_root, issue_number, raw_snapshot, planner_input_dict, result)
        # Issue #2052 AC7: a purely-additive, best-effort side artifact --
        # never one of the three fixed-schema artifacts above, and never
        # allowed to turn a successful preflight run into a failure. Only
        # written when the caller opted into the evidence cache AND the
        # shared context_budget_report module imported successfully.
        if evidence_index is not None and _ContextBudgetReport is not None:
            try:
                _report = _ContextBudgetReport(consumer="run_refinement_preflight.py")
                _report.record_from_evidence_index(EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH, evidence_index)
                _report.write_json(_issue_artifact_dir(repo_root, issue_number) / "context_budget_report.json")
            except Exception:  # pragma: no cover - defensive, never fails the run
                pass

    except Exception as exc:
        result = _build_result(
            status="environment_failure",
            issue_number=issue_number,
            repo=repo,
            planner_exit_code=planner_exit_code,
            planner_fail_closed=planner_fail_closed,
            next_action="fix_environment",
            must_read=sorted(set(must_read)),
            do_not_read=do_not_read,
            commands=commands,
            blockers=[
                *blockers,
                BLOCKER_RESULT_SCHEMA_INVALID,
                f"result_artifact_write_error:{type(exc).__name__}:{str(exc)[:500]}",
            ],
            planner_fail_closed_reason_codes=planner_fail_closed_reason_codes,
            required_sections=required_sections,
            required_contract_keys=required_contract_keys,
            rewrite_constraints=rewrite_constraints,
            artifacts={},
            hashes=hashes,
            # Issue #2257 AC10: this is a result-artifact write failure, not
            # a transport read failure -- `source`/`operation` reflect that
            # distinction rather than echoing the (possibly stale) transport
            # source used earlier in this same invocation.
            environment_failure_reason_code="transport_internal_error",
            environment_failure_source="internal",
            environment_failure_operation="write_artifacts",
        )
        exit_code = EXIT_ENVIRONMENT_FAILURE

    # --- Blocker 1 (success path): provenance sidecar ---
    try:
        _anchor_url_prov = anchor_comment_urls[0] if anchor_comment_urls else ""
        _provenance = build_provenance(
            repo=repo,
            issue_number=issue_number,
            anchor_comment_url=_anchor_url_prov,
            planner_input=planner_input_dict,
            raw_snapshot=raw_snapshot,
            wrapper_exit_code=exit_code,
            wrapper_status=result.get("status", "unknown"),
            blockers=blockers,
            stderr=planner_stderr or "",
            repo_root=repo_root,
            plan=plan,
            result_next_action=result.get("next_action", "unavailable"),
        )
        write_provenance_artifact(repo_root, issue_number, _provenance)
    except Exception:
        pass

    # Print compact stdout (no raw body/comments/sentinels) — same result dict
    print(_build_compact_stdout(result))

    return result, exit_code


# ---------------------------------------------------------------------------
# Provenance and failure classification (Issue #1035)
# ---------------------------------------------------------------------------


def _git_head_sha(repo_root: Path) -> str:
    """Return git HEAD SHA or 'unknown' on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# known_context["main_drift"] live production producer (Issue #2102
# fix_delta, iteration 4, Blocker 4). This is the orchestrator step the
# `known_context["main_drift"] production contract` comment in
# `plan_refinement_loop.py` documents as missing wiring: it performs the
# live git readback (bounded `git fetch` / `rev-parse` / `diff` / `merge-tree
# --write-tree`) and builds the dict that function's docstring requires,
# then `run_preflight()` below merges it into `known_context` before
# `plan_refinement_loop.py` is invoked. Every subprocess call is bounded and
# fails closed to `None` (never a fabricated/partial dict) on any timeout,
# non-zero exit, or missing origin remote.
# ---------------------------------------------------------------------------


def _run_git_readonly_bounded(argv: list, cwd: Path, timeout: int = 20):
    """Bounded, fail-closed `git` readback helper (returns None on any
    timeout/launch failure instead of raising -- callers must treat `None`
    as "cannot confirm", never as a stand-in for a real result)."""
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


# Issue #2783: minimal, self-contained equivalent of
# `scripts/agent-ops/allowed_paths_policy.py::is_no_path_marker()`,
# deliberately DUPLICATED here (not imported) rather than given a hard
# import dependency on `scripts/agent-ops/` -- some regression fixtures
# (`test_command_registry.py`'s
# `TestAuthorityTransportPrivilegedExecutorRealSubprocessDispatch`,
# `test_owner_reaction_not_planned_gate_production_shaped.py`) construct an
# isolated fixture git repo containing only a hand-picked subset of files
# (this script, `command_registry.py`, `decide_next_loop_action.py`,
# `schemas/`) WITHOUT `scripts/agent-ops/`, and spawn this script as a REAL
# subprocess against that fixture repo. A hard `from allowed_paths_policy
# import ...` here breaks those fixtures at process-start time with
# `ModuleNotFoundError` (caught during this Issue's own verification --
# the exact failure mode the module's pre-existing "no import boundary
# into scripts/agent-ops/" design note already warned about). This
# duplication's exact two marker strings are covered by
# `test_extract_allowed_paths_from_issue_body_no_path_marker_returns_empty`
# in `test_refinement_preflight.py`.
_NO_PATH_MARKERS_LOCAL = ("(none)", "読み取り専用。リポジトリ変更なし（既定）")


def _is_no_path_marker_local(entry: str) -> bool:
    s = entry.strip()
    if s[:1] in ("-", "+", "*"):
        s = s[1:].strip()
    if len(s) >= 2 and s.startswith("`") and s.endswith("`") and s.count("`") == 2:
        s = s[1:-1].strip()
    return s in _NO_PATH_MARKERS_LOCAL


def _extract_allowed_paths_from_issue_body(body: str) -> list:
    """Parse the canonical `## Allowed Paths` bullet list the same shape
    `pr_head_replay_publish_exec.py::_allowed_paths()` already parses on the
    implementation-loop side (kept independently here since this module has
    no import boundary into `scripts/agent-ops/`). Issue #2783: canonical
    marker `(none)` / legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` both resolve to 0 paths, via the
    local `_is_no_path_marker_local()` duplicate above (previously this
    function had no annotation stripping at all, so both markers leaked as
    literal non-empty "path" strings)."""
    marker = "## Allowed Paths"
    if marker not in body:
        return []
    section = body.split(marker, 1)[1].split("\n## ", 1)[0]
    paths = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        # Issue #2783: judge no-path marker BEFORE stripping the trailing
        # backtick wrapper -- a marker line contributes 0 paths.
        if _is_no_path_marker_local(stripped):
            continue
        candidate = stripped[1:].strip().strip("`")
        if candidate:
            paths.append(candidate)
    return paths


def build_live_main_drift_known_context(
    *,
    repo_root: Path,
    issue_body: str,
    base_ref: str = "main",
    evidence_base_sha: Optional[str] = None,
    allowed_paths_snapshot_base_sha: Optional[str] = None,
    expected_head_sha: Optional[str] = None,
    observed_head_sha: Optional[str] = None,
    expected_old_sha: Optional[str] = None,
    observed_old_sha: Optional[str] = None,
) -> Optional[dict]:
    """Build `known_context["main_drift"]` from a live git readback (Issue
    #2102 fix_delta iteration 4, Blocker 4; CAS-pair defaults corrected in
    fix_delta iteration 5, Blocker A). See the production contract comment
    above `_refinement_main_drift_decision()` in `plan_refinement_loop.py`
    for the exact key contract this satisfies, and
    `route_loop_verdict_v2.classify_main_drift()` for the consumer that
    validates every key below is present and well-formed.

    Fails closed to `None` (never a dict built from partial/stale evidence)
    if the live `origin/<base_ref>` readback, or (when the evidence epoch
    actually differs from the live base) the `git diff` / `git merge-tree
    --write-tree` probes, cannot be completed within their bounded
    timeouts. Callers MUST treat a `None` return as "no main_drift evidence
    available this cycle" -- never as "no drift detected".

    `expected_head_sha`/`observed_head_sha` and `expected_old_sha`/
    `observed_old_sha` are CAS guards `classify_main_drift()` uses to
    reject a *concurrent* mutation of whatever ref this evidence rebind
    will ultimately touch -- they are a distinct concern from the
    `current_base_sha` vs `evidence_base_sha` drift comparison performed
    below. This producer has no independent prior-known state to CAS
    against (it is a read-only diagnostic epoch classification, not the
    mutation itself), so every pair defaults to the SAME just-read live
    value unless a caller supplies an explicit prior expectation to guard
    against a race. Defaulting `expected_old_sha` to `evidence_base_sha`
    while `observed_old_sha` defaulted to `current_base_sha` (the
    pre-fix_delta-5 behavior) made these two CAS pairs mismatch on every
    genuine drift, which caused `classify_main_drift()` to hard_stop with
    `expected_old_cas_mismatch` on all drifted input -- silently defeating
    AC1's scope-clean reconciliation route before it could ever be
    reached in production.
    """
    fetched = _run_git_readonly_bounded(
        ["fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{base_ref}"], repo_root, timeout=20
    )
    if fetched is None or fetched.returncode != 0:
        return None
    current = _run_git_readonly_bounded(["rev-parse", f"origin/{base_ref}"], repo_root, timeout=10)
    if current is None or current.returncode != 0:
        return None
    current_base_sha = current.stdout.strip()
    if not current_base_sha:
        return None

    resolved_evidence_base_sha = evidence_base_sha or current_base_sha
    resolved_allowed_paths_snapshot_base_sha = allowed_paths_snapshot_base_sha or resolved_evidence_base_sha
    resolved_expected_head_sha = expected_head_sha or current_base_sha
    resolved_observed_head_sha = observed_head_sha or resolved_expected_head_sha
    resolved_expected_old_sha = expected_old_sha or current_base_sha
    resolved_observed_old_sha = observed_old_sha or resolved_expected_old_sha

    allowed_paths = _extract_allowed_paths_from_issue_body(issue_body)

    latest_main_net_diff: list = []
    semantic_ambiguity = False
    if resolved_evidence_base_sha != current_base_sha:
        diff = _run_git_readonly_bounded(
            ["diff", "--name-only", resolved_evidence_base_sha, current_base_sha], repo_root, timeout=20
        )
        if diff is None or diff.returncode != 0:
            return None
        latest_main_net_diff = [line for line in diff.stdout.splitlines() if line]

        # Deterministic real-conflict oracle (mirrors
        # `pr_head_replay_publish_exec.py::_merge_tree_conflicts()` on the
        # implementation-loop side, Issue #2102 P1-C): a nonzero exit from
        # the two-ref `git merge-tree --write-tree` form means the merge
        # produced conflicts. This is never a caller-asserted boolean.
        merge_probe = _run_git_readonly_bounded(
            ["merge-tree", "--write-tree", resolved_evidence_base_sha, current_base_sha], repo_root, timeout=20
        )
        if merge_probe is None:
            return None
        semantic_ambiguity = merge_probe.returncode != 0

    return {
        "current_base_sha": current_base_sha,
        "evidence_base_sha": resolved_evidence_base_sha,
        "allowed_paths_snapshot_base_sha": resolved_allowed_paths_snapshot_base_sha,
        "allowed_paths": allowed_paths,
        "latest_main_net_diff": latest_main_net_diff,
        "expected_head_sha": resolved_expected_head_sha,
        "observed_head_sha": resolved_observed_head_sha,
        "expected_old_sha": resolved_expected_old_sha,
        "observed_old_sha": resolved_observed_old_sha,
        "semantic_ambiguity": semantic_ambiguity,
    }


def _git_blob_sha(file_path: Path, repo_root: Path) -> str:
    """Return git blob SHA of a file or 'unknown' on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "hash-object", str(file_path.resolve())],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_head_tree_blob_sha(file_path: Path, repo_root: Path) -> str:
    """Return blob SHA from HEAD tree (git rev-parse HEAD:<relpath>) or 'unknown'."""
    try:
        relpath = file_path.resolve().relative_to(repo_root.resolve())
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{relpath}"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_worktree_status(file_path: Path, repo_root: Path) -> str:
    """Return 'git status --short' for a file or 'unknown' on failure."""
    try:
        relpath = file_path.resolve().relative_to(repo_root.resolve())
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short", "--", str(relpath)],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_py_compile_proof(script_path: Path, repo_root: Path) -> dict:
    """Generate PY_SYNTAX_COMPILE_PROOF_V2 artifact for a Python script.

    Compiles the source in-process without materializing a bytecode cache.
    ``python -m py_compile`` is intentionally not used here: it writes a
    ``__pycache__`` entry next to the source even when descendant imports have
    ``PYTHONDONTWRITEBYTECODE`` enabled.  The privileged executor must retain
    source-tree write attribution, so provenance generation cannot create an
    otherwise unauthorized cache file.

    The source is read and compiled as raw ``bytes`` (not a decoded ``str``)
    so that CPython's own PEP 263 encoding-declaration handling (UTF-8 BOM,
    ``# -*- coding: ... -*-`` cookie, and BOM/cookie contradictions) applies
    exactly as it would for a normally-imported module, instead of this
    wrapper silently assuming UTF-8. ``dont_inherit=True`` and ``flags=0``
    ensure this module's own ``from __future__ import annotations`` (and any
    other wrapper-side future statement) is never inherited by the checked
    script. There is no executable ``command`` for this proof: the operation
    is in-process and is described by ``operation_kind`` instead.
    """
    script_realpath = str(script_path.resolve())
    operation_kind = "in_process_compile"
    source_mode = "bytes"
    flags = 0
    dont_inherit = True
    optimize = -1

    try:
        source_bytes = script_path.read_bytes()
        compile(
            source_bytes,
            script_realpath,
            "exec",
            flags=flags,
            dont_inherit=dont_inherit,
            optimize=optimize,
        )
        py_compile_status = "pass"
        stderr_text = ""
    except Exception as exc:
        py_compile_status = "fail"
        stderr_text = str(exc)

    stderr_excerpt = stderr_text[:500]

    return {
        "schema_version": "PY_SYNTAX_COMPILE_PROOF_V2",
        "operation_kind": operation_kind,
        "source_mode": source_mode,
        "flags": flags,
        "dont_inherit": dont_inherit,
        "optimize": optimize,
        "cache_write_expected": False,
        "py_compile_status": py_compile_status,
        "python_version": sys.version,
        "python_executable": sys.executable,
        "git_head_sha": _git_head_sha(repo_root),
        "planner_script_path": str(script_path),
        "planner_script_realpath": script_realpath,
        "planner_script_blob_sha": _git_blob_sha(script_path, repo_root),
        "cwd": str(Path.cwd()),
        "stderr_sha256": _sha256(stderr_text),
        "stderr_excerpt": stderr_excerpt,
    }


def classify_planner_failure(
    exit_code: int,
    stdout: str,
    stderr: str,
    script_path: Optional[Path] = None,
    python_executable: Optional[str] = None,
) -> dict:
    """Classify a planner failure into PLANNER_FAILURE_CLASSIFICATION_V1 taxonomy.

    Categories (mutually exclusive, evaluated in priority order):
      syntax_compile_failure         - SyntaxError detected in stderr / exit != 0
      anchor_or_input_blocked        - exit 2 (invalid input / schema error)
      planner_stdout_non_json        - exit 0 but stdout is not valid JSON
      wrapper_environment_failure    - gh not found / auth / timeout
      planner_runtime_internal_error - exit 3 without SyntaxError
    """
    stderr_str = stderr or ""
    stdout_str = stdout or ""
    is_syntax_error = bool(re.search(r"SyntaxError|py_compile\b", stderr_str))

    if is_syntax_error and exit_code != 0:
        category = "syntax_compile_failure"
    elif exit_code == 2:
        category = "anchor_or_input_blocked"
    elif exit_code == 0:
        try:
            json.loads(stdout_str)
            category = "planner_runtime_internal_error"
        except (json.JSONDecodeError, ValueError):
            category = "planner_stdout_non_json"
    elif exit_code == 3:
        env_keywords = ("not found", "FileNotFoundError", "timeout", "gh_", "gh not")
        if any(kw in stderr_str for kw in env_keywords):
            category = "wrapper_environment_failure"
        else:
            category = "planner_runtime_internal_error"
    else:
        category = "wrapper_environment_failure"

    # Traceback excerpt (SyntaxError only)
    traceback_excerpt = ""
    if category == "syntax_compile_failure":
        lines = stderr_str.splitlines()
        relevant = [
            ln for ln in lines if "SyntaxError" in ln or ln.strip().startswith("File ") or "line " in ln.lower()
        ]
        traceback_excerpt = "\n".join(relevant[:10])

    # JSON decode error (non-JSON stdout only)
    json_decode_error = ""
    if category == "planner_stdout_non_json":
        try:
            json.loads(stdout_str)
        except (json.JSONDecodeError, ValueError) as exc:
            json_decode_error = str(exc)

    script_realpath = str(script_path.resolve()) if script_path else ""

    return {
        "schema_version": "PLANNER_FAILURE_CLASSIFICATION_V1",
        "category": category,
        "exit_code": exit_code,
        "stdout_sha256": _sha256(stdout_str),
        "stderr_sha256": _sha256(stderr_str),
        "stderr_excerpt": stderr_str[:500],
        "json_decode_error": json_decode_error,
        "traceback_excerpt": traceback_excerpt,
        "script_path": str(script_path) if script_path else "",
        "script_realpath": script_realpath,
        "python_executable": python_executable or sys.executable,
        "python_version": sys.version,
    }


def build_provenance(
    repo: str,
    issue_number: int,
    anchor_comment_url: str,
    planner_input: dict,
    raw_snapshot: dict,
    wrapper_exit_code: int,
    wrapper_status: str,
    blockers: list,
    stderr: str,
    repo_root: Path,
    plan: Optional[dict] = None,
    result_next_action: str = "unavailable",
) -> dict:
    """Generate REFINEMENT_PREFLIGHT_PROVENANCE_V1 sidecar artifact.

    Captures the full execution context of a preflight run so that a later
    replay or audit can verify which file/interpreter/commit was used.
    Written to the same artifact directory as the main result but as a
    separate file (``refinement_preflight_provenance_v1.json``) to avoid
    violating the strict ``additionalProperties: false`` result schema.
    """
    planner_script = PLANNER_SCRIPT
    wrapper_script = Path(__file__).resolve()

    py_compile_proof = build_py_compile_proof(planner_script, repo_root)

    planner_input_text = _canonical_json(planner_input)
    raw_snapshot_text = _canonical_json(raw_snapshot)
    stderr_str = stderr or ""
    sidecar = (plan or {}).get("scope_signal_guard_decision_v2")
    authority = sidecar.get("scope_delta_authority") if isinstance(sidecar, dict) else {}
    authority_route = authority.get("route") if isinstance(authority, dict) else {}
    if not isinstance(authority_route, dict):
        # A malformed/scalar route must never suppress the provenance sidecar.
        # Preserve fail-closed semantics by recording no implementation route
        # rather than treating the scalar as an authorization object.
        authority_route = {}
    known_context = planner_input.get("known_context") if isinstance(planner_input, dict) else {}
    evidence_list = (
        known_context.get("scope_delta_authority_evidence")
        if isinstance(known_context, dict)
        else []
    )
    source_evidence = evidence_list[0] if isinstance(evidence_list, list) and evidence_list else {}
    if not isinstance(source_evidence, dict):
        source_evidence = {}
    runtime_evidence = {
        # The provenance sidecar is intentionally additive to the strict
        # preflight-result schema. It binds runtime observations but cannot
        # itself authorize implementation.
        "tested_head_sha": _git_head_sha(repo_root),
        "source": {
            "comment_url": source_evidence.get("comment_url"),
            "comment_id": source_evidence.get("comment_id"),
            "body_sha256": source_evidence.get("body_sha256"),
            "source_kind": source_evidence.get("source_kind"),
        },
        "route": {
            "action": authority_route.get("action"),
            "implementation_allowed": authority_route.get("implementation_allowed"),
            "required_rerun": authority_route.get("next_step"),
        },
        "terminal_event": {
            "wrapper_status": wrapper_status,
            "next_action": result_next_action,
            "implementation_allowed": authority_route.get("implementation_allowed"),
        },
        # The generic preflight cannot claim a #1952-specific profile check
        # has passed. Consumers must execute that validator at the current
        # head before implementation; until then this is an explicit deny.
        "permission_profile_validators": {
            "status": "required_before_implementation",
            "passed": False,
        },
    }

    def _dependency_version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "not_installed"

    return {
        "schema_version": "REFINEMENT_PREFLIGHT_PROVENANCE_V1",
        "repo": repo,
        "issue_number": issue_number,
        "anchor_comment_url": anchor_comment_url,
        "git_head_sha": _git_head_sha(repo_root),
        "planner_invocation_command": [sys.executable, str(planner_script)],
        "planner_script_path": str(planner_script),
        "planner_script_realpath": str(planner_script.resolve()),
        "planner_script_blob_sha": _git_blob_sha(planner_script, repo_root),
        "planner_head_tree_blob_sha": _git_head_tree_blob_sha(planner_script, repo_root),
        "planner_worktree_status": _git_worktree_status(planner_script, repo_root),
        "wrapper_script_blob_sha": _git_blob_sha(wrapper_script, repo_root),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "dependency_versions": {
            "jsonschema": _dependency_version("jsonschema"),
            "referencing": _dependency_version("referencing"),
        },
        "cwd": str(Path.cwd()),
        "py_compile_status": py_compile_proof["py_compile_status"],
        # Issue #1439 AC12: persist the full PY_SYNTAX_COMPILE_PROOF_V2 proof
        # (schema_version + all required fields), not just the collapsed
        # py_compile_status summary above, so a later reader of the
        # refinement_preflight_provenance_v1.json artifact can verify the
        # full V2 semantics without re-deriving them.
        "python_syntax_compile_proof": py_compile_proof,
        "wrapper_exit_code": wrapper_exit_code,
        "wrapper_status": wrapper_status,
        "blockers": list(blockers),
        "planner_input_sha256": _sha256(planner_input_text),
        "raw_snapshot_sha256": _sha256(raw_snapshot_text),
        "stderr_sha256": _sha256(stderr_str),
        "stderr_excerpt": stderr_str[:500],
        "runtime_evidence": runtime_evidence,
    }


def build_replay_proof(
    live_input: dict,
    fixture_input: dict,
    live_result_status: str,
    fixture_result_status: str,
) -> dict:
    """Generate REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1.

    Compares the SHA256 of the canonical JSON of ``live_input`` (fetched
    from GitHub) against ``fixture_input`` (a saved snapshot).  Identical
    hashes guarantee the classification is deterministic; a mismatch is
    classified as ``input_drift`` so that the caller cannot prematurely
    declare the issue resolved.
    """
    live_sha = _sha256(_canonical_json(live_input))
    fixture_sha = _sha256(_canonical_json(fixture_input))
    input_drift_detected = live_sha != fixture_sha
    results_consistent = live_result_status == fixture_result_status

    if input_drift_detected:
        classification = "input_drift"
    elif results_consistent:
        classification = "replay_consistent"
    else:
        classification = "classification_mismatch"

    return {
        "schema_version": "REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1",
        "live_input_sha256": live_sha,
        "fixture_input_sha256": fixture_sha,
        "input_drift_detected": input_drift_detected,
        "live_result_status": live_result_status,
        "fixture_result_status": fixture_result_status,
        "results_consistent": results_consistent,
        "classification": classification,
    }


def write_provenance_artifact(
    repo_root: Path,
    issue_number: int,
    provenance: dict,
) -> str:
    """Write provenance dict to the artifacts directory and return the path."""
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prov_path = artifact_dir / "refinement_preflight_provenance_v1.json"
    prov_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return str(prov_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------



def _repository_identity_blocker(repo: object) -> str | None:
    """Blocker code unless `repo` is exactly the trusted slug from the single identity
    config (fail closed: unusable config or a missing loader is a blocker, never a pass)."""
    if _repo_identity is None:
        return BLOCKER_REPOSITORY_IDENTITY_UNAVAILABLE
    try:
        trusted = _repo_identity.is_trusted_repo(repo)
    except _repo_identity.RepositoryIdentityError:
        return BLOCKER_REPOSITORY_IDENTITY_UNAVAILABLE
    return None if trusted else BLOCKER_REPOSITORY_IDENTITY_MISMATCH


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic preflight wrapper for issue-refinement-loop.",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub Issue number (positive int).")
    parser.add_argument("--repo", required=True, help="owner/repo string (must match ^[^/]+/[^/]+$).")
    parser.add_argument(
        "--anchor-comment-url",
        dest="anchor_comment_urls",
        action="append",
        default=[],
        help=(
            "Anchor comment URL to validate. The CLI flag is repeatable, but only "
            "0 or 1 distinct URL is actually supported: passing 2+ distinct "
            "anchor-comment URLs is rejected as ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED "
            "(repeating the identical URL is deduplicated to 1 and is not an "
            "error)."
        ),
    )
    parser.add_argument(
        "--human-context-comment-url",
        dest="human_context_comment_urls",
        action="append",
        default=[],
        help="Explicit human-context issue-comment URL (repeatable).",
    )
    parser.add_argument(
        "--agent-report-comment-url",
        dest="agent_report_comment_urls",
        action="append",
        default=[],
        help="Explicit agent-report issue-comment URL (repeatable; never authorizes a rewrite).",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to fixture JSON (bypasses gh CLI calls).",
    )
    parser.add_argument(
        "--consume-contract-patch-plan",
        action="store_true",
        help="Execute a trusted CONTRACT_PATCH_PLAN_V1 through edit_issue_txn.py.",
    )
    parser.add_argument(
        "--evidence-cache",
        dest="evidence_cache_enabled",
        action="store_true",
        default=False,
        help="Issue #2052: opt in to phase-scoped SHA-bound reuse of this invocation's "
        "own Issue/comment fetch results (never a generic command cache; never persisted "
        "across process invocations). Default off -- every pre-existing caller's behavior "
        "is unchanged. When on, a purely-additive context_budget_report.json artifact "
        "(observed fetch_count/emitted_utf8_bytes/snapshot_reuse_count/"
        "duplicate_projection_count only, never a fabricated token/model-turn figure) is "
        "written alongside the existing raw_issue_snapshot.json / planner_input.json / "
        "refinement_preflight_result_v1.json artifacts.",
    )
    parser.add_argument(
        "--disable-main-drift-live-readback",
        dest="enable_main_drift_live_readback",
        action="store_false",
        default=True,
        help="Issue #2102 fix_delta (iteration 5, Blocker A): the CLI/registry "
        "production entrypoint (command_registry.py's 'preflight.run' family, which "
        "never renders this flag) now performs the bounded live git readback "
        "(fetch/diff/merge-tree against 'origin') to populate known_context['main_drift'] "
        "before invoking the planner BY DEFAULT -- opt-out via this flag, not opt-in. "
        "The prior opt-in default (Blocker 4, iteration 4) never actually enabled this "
        "in production because no registered command_registry.py entry rendered the "
        "old --enable-main-drift-live-readback flag, leaving the producer dead code; "
        "see build_live_main_drift_known_context()'s docstring for the CAS-pair-default "
        "correctness fix (iteration 5) that made this safe to flip. Only the CLI/main() "
        "entrypoint's default changed -- run_preflight()'s own Python-level keyword "
        "default is unchanged (False) so every other in-process caller (e.g. "
        "_apply_contract_patch_plan_v1()'s fresh_checks() post-mutation reverification "
        "helper, which does not have every skill's sibling scripts/ directory on "
        "sys.path in every test harness) keeps its pre-existing behavior unless it "
        "explicitly opts in.",
    )
    parser.add_argument(
        "--investigation-evidence-transport-path",
        type=Path,
        default=None,
        metavar="MANIFEST_JSON_PATH",
        help="#2086 P0 fix_delta (Blocker 1/2): path to a "
        "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest (minted via "
        "--produce-authority-transport / the authority_transport.produce "
        "command_id) carrying a bound, digest-verified read-only-investigation "
        "exact-path inventory. Only ever consulted for the operator-selected "
        "human-context lane; see _validate_investigation_evidence_transport().",
    )
    parser.add_argument(
        "--investigation-evidence-primary-root",
        type=Path,
        default=None,
        metavar="PRIMARY_ROOT_ABS_PATH",
        help="Issue #2199 OWNER feedback P1-3: absolute path of the PRIMARY "
        "checkout `--investigation-evidence-transport-path` is repo-relative "
        "to. Only ever set by the privileged executor (skill_runtime_exec.py) "
        "for the 4 production preflight profiles, which run this script's cwd "
        "at the #2197/#2199 dedicated worktree, not the primary checkout -- "
        "without this, the SAME repo-relative transport-path string resolves "
        "against the dedicated worktree's own root (_find_repo_root(), "
        "derived from this script's own dedicated-checkout __file__) instead "
        "of the primary root the caller actually validated it against, "
        "silently missing the real evidence file. When omitted (every other "
        "command_id, and any non-privileged direct invocation), behavior is "
        "byte-identical to before this flag existed: the transport path "
        "resolves against this run's own _find_repo_root().",
    )
    parser.add_argument(
        "--invocation-id",
        default=None,
        metavar="ID",
        help="#2053: invocation identifier bound into SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 / receipts.",
    )
    parser.add_argument(
        "--git-head-sha",
        default=None,
        metavar="SHA",
        help="#2053: git HEAD sha bound into SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 / receipts "
        "(defaults to the live repo HEAD when omitted).",
    )
    parser.add_argument(
        "--produce-authority-transport",
        type=Path,
        default=None,
        metavar="EVIDENCE_JSON_PATH",
        help="#2053 AC1/AC7: read a SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (or list) JSON file and emit "
        "an immutable SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest (producer role). Requires "
        "--issue-number, --repo, --invocation-id.",
    )
    parser.add_argument(
        "--consume-authority-transport",
        default=None,
        metavar="ROUTER_RECEIPT_PATH",
        help="#2053 AC9: read a SCOPE_DELTA_ROUTER_RECEIPT_V1 and perform the controlled consumer "
        "role (verify digest, mutate once, readback, fresh rerun), emitting "
        "SCOPE_DELTA_CONSUMPTION_RECEIPT_V1. Requires --issue-number, --repo, --invocation-id, "
        "--git-head-sha.",
    )
    parser.add_argument(
        "--contract-patch-plan-file",
        type=Path,
        default=None,
        metavar="CONTRACT_PATCH_PLAN_JSON_PATH",
        help="#2053 P0 fix-delta (iteration 3): only meaningful with "
        "--consume-authority-transport. Path to a CONTRACT_PATCH_PLAN_V1 JSON file; when "
        "supplied together with --anchor-context-file, the consumer's mutation is delegated "
        "to the real controlled-mutation lane (consume_trusted_anchor_contract_patch_plan -> "
        "edit_issue_txn.py) instead of only writing the local audit artifact.",
    )
    parser.add_argument(
        "--anchor-context-file",
        type=Path,
        default=None,
        metavar="ANCHOR_CONTEXT_JSON_PATH",
        help="#2053 P0 fix-delta (iteration 3): only meaningful with "
        "--consume-authority-transport and --contract-patch-plan-file. Path to a JSON file "
        "with keys `issue`, `anchor_url`, `anchor_payload`, `anchor_body` "
        "(and optionally `known_context`).",
    )

    parser.add_argument(
        "--apply-repair-action",
        type=Path,
        default=None,
        metavar="PREFLIGHT_RESULT_JSON_PATH",
        help="Issue #2039 AC8/AC11: read a previously-produced preflight result "
        "JSON artifact (FD-based secure open), resolve the exactly-one mutation "
        "intent, and -- only when repair_action.disposition == auto_apply_safe -- "
        "dispatch the repaired body through edit_issue_txn.py --input-file "
        "(never a raw `gh issue edit` call). Requires --issue-number, --repo.",
    )

    parser.add_argument(
        "--apply-structural-repair-action",
        type=Path,
        default=None,
        metavar="PREFLIGHT_RESULT_JSON_PATH",
        help="Issue #2396: read a previously-produced preflight result JSON "
        "artifact (FD-based secure open), and -- only when "
        "structural_repair_action.disposition_summary == auto_apply_safe with "
        "every item itself auto_apply_safe and no ambiguous insertion present -- "
        "re-verify each item's digests against the live Issue body, synthesize a "
        "single repaired body, and dispatch it through edit_issue_txn.py "
        "--input-file (the SAME shared core --apply-repair-action uses; never a "
        "raw `gh issue edit` call). Requires --issue-number, --repo.",
    )

    # Issue #2689 P0-1 fix_delta (PR #2697 OWNER review comment
    # #5755475318): canonical production transport for the heavy mutation
    # gate's `mutation_category` / `owner_reaction_context` -- before this,
    # no command_registry.py-rendered production argv ever populated
    # `known_context["mutation_category"]`, so
    # `_classify_heavy_mutation_gate_with_fresh_owner_reaction()` was
    # unreachable from the real CLI entrypoint regardless of how it was
    # dispatched. `--mutation-category`'s choices are the SAME 5 literal
    # heavy mutation categories `HEAVY_MUTATION_CATEGORIES` already declares
    # -- this flag never opens a NEW category; only `not_planned` is
    # actually approvable via the owner-reaction path (see
    # `_classify_heavy_mutation_gate()`). `--owner-user-id` /
    # `--preview-binding-file` mirror the SAME `owner_reaction.decide`
    # placeholder shapes `command_registry.py` already declares.
    parser.add_argument(
        "--mutation-category",
        dest="mutation_category",
        choices=sorted(HEAVY_MUTATION_CATEGORIES),
        default=None,
        metavar="CATEGORY",
        help="Issue #2689: intended heavy-mutation category for this invocation "
        "(one of HEAVY_MUTATION_CATEGORIES). Only ever consulted by the "
        "pre-existing #1891 heavy mutation gate; never widens which categories "
        "the gate actually approves. Requires --owner-user-id and "
        "--preview-binding-file to also be supplied (all three together, or none).",
    )
    parser.add_argument(
        "--owner-user-id",
        dest="owner_user_id",
        type=int,
        default=None,
        metavar="USER_ID",
        help="Issue #2689: stable GitHub user id of the OWNER whose reaction the "
        "fresh owner_reaction_decision.py subprocess resolves against. Only "
        "meaningful together with --mutation-category and "
        "--preview-binding-file.",
    )
    parser.add_argument(
        "--preview-binding-file",
        dest="preview_binding_file",
        default=None,
        metavar="PREVIEW_BINDING_JSON_PATH",
        help="Issue #2689: repo-relative path to the PREVIEW_BINDING_V1 artifact "
        "owner_reaction_decision.py binds its selection against. Only "
        "meaningful together with --mutation-category and --owner-user-id.",
    )

    args = parser.parse_args(argv)

    # Repository binding runs before every mode (fixture, live, produce/consume):
    # a repository other than the trusted one never reaches planning.
    _identity_blocker = _repository_identity_blocker(args.repo)
    if _identity_blocker is not None:
        print(
            _build_compact_stdout(
                _build_result(
                    status="blocked",
                    issue_number=args.issue_number or 0,
                    repo="",
                    planner_exit_code=None,
                    planner_fail_closed=None,
                    next_action="human_judgment_required",
                    must_read=[],
                    do_not_read=[],
                    commands=[],
                    blockers=[_identity_blocker],
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                    artifacts={},
                    hashes={},
                )
            )
        )
        sys.exit(
            EXIT_ENVIRONMENT_FAILURE
            if _identity_blocker == BLOCKER_REPOSITORY_IDENTITY_UNAVAILABLE
            else EXIT_BLOCKED
        )

    # #2053: producer / consumer dedicated CLI modes -- bypass the full
    # gh-backed preflight pipeline entirely (parallel sibling entries, same
    # pattern as --fixture; see command_registry.py "authority_transport.produce"
    # / "authority_transport.consume").
    if args.produce_authority_transport is not None:
        _repo_root = _find_repo_root()
        try:
            evidence_payload = json.loads(args.produce_authority_transport.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "environment_failure", "reason_code": "malformed_json", "error": str(exc)}))
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        git_head_sha = args.git_head_sha or _git_head_sha(_repo_root)
        result, error = generate_authority_transport_manifest(
            evidence=evidence_payload,
            issue_number=args.issue_number,
            repo=args.repo,
            invocation_id=args.invocation_id or "",
            git_head_sha=git_head_sha,
            repo_root=_repo_root,
        )
        if result is None:
            print(json.dumps({"status": "environment_failure", "reason_code": "write_failure", "error": error}))
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2, sort_keys=True))
        sys.exit(0)

    if args.apply_repair_action is not None:
        # PR #2202 review fix (P0-2 bullet 3): the production CLI entrypoint
        # previously never constructed `expected_provenance` at all -- only
        # test code injected it -- so the provenance-binding check inside
        # run_repair_action_apply() was effectively unreachable in
        # production beyond its own always-on repo/issue self-consistency
        # check. Derive it here from the SAME artifact's own
        # producer-emitted provenance fields (secure-read via the same
        # FD-based reader run_repair_action_apply() itself uses) plus the
        # live repo/issue identity this invocation was given, so a
        # mid-flight artifact replacement between this read and
        # run_repair_action_apply()'s own internal read is still caught as
        # a mismatch rather than silently accepted.
        _cli_repo_root = _find_repo_root()
        _cli_expected_provenance: "dict | None" = None
        try:
            _cli_pf_path = Path(str(args.apply_repair_action))
            if not _cli_pf_path.is_absolute():
                _cli_pf_path = _cli_repo_root / _cli_pf_path
            _cli_text, _ = secure_read_repair_apply_artifact(_cli_pf_path, root=_cli_repo_root)
            _cli_parsed = json.loads(_cli_text)
            _cli_repair_action = _cli_parsed.get("repair_action") if isinstance(_cli_parsed, dict) else None
            if isinstance(_cli_repair_action, dict):
                _cli_expected_provenance = {
                    "repo": args.repo,
                    "issue_number": args.issue_number,
                    "preflight_run_identity": _cli_repair_action.get("preflight_run_identity"),
                    "original_body_sha256": _cli_repair_action.get("original_body_sha256"),
                    "repair_action_core_sha256": _repair_action_core_sha256(_cli_repair_action),
                }
        except (RepairApplySecureOpenError, json.JSONDecodeError, OSError):
            _cli_expected_provenance = None

        result = run_repair_action_apply(
            repo=args.repo,
            issue_number=args.issue_number,
            preflight_result_path=str(args.apply_repair_action),
            expected_provenance=_cli_expected_provenance,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        outcome = result.get("mutation_outcome")
        # PR #2202 review fix-delta (P0-5): a real fresh-validation failure
        # (phase == "fresh_validation" with a non-null failure_code) must
        # exit non-zero even when the mutation itself genuinely applied --
        # mutation_outcome alone previously hid this failure behind exit 0.
        if result.get("phase") == "fresh_validation" and result.get("failure_code"):
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        if outcome in {"applied", "no_change"}:
            sys.exit(0)
        if outcome == "not_attempted":
            sys.exit(EXIT_BLOCKED)
        sys.exit(EXIT_ENVIRONMENT_FAILURE)

    if args.apply_structural_repair_action is not None:
        result = run_structural_repair_action_apply(
            repo=args.repo,
            issue_number=args.issue_number,
            preflight_result_path=str(args.apply_structural_repair_action),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        outcome = result.get("mutation_outcome")
        if result.get("phase") == "fresh_validation" and result.get("failure_code"):
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        if outcome in {"applied", "no_change"}:
            sys.exit(0)
        if outcome == "not_attempted":
            sys.exit(EXIT_BLOCKED)
        sys.exit(EXIT_ENVIRONMENT_FAILURE)

    if args.consume_authority_transport is not None:
        _repo_root = _find_repo_root()
        git_head_sha = args.git_head_sha or _git_head_sha(_repo_root)
        contract_patch_plan_arg = None
        anchor_context_arg = None
        if args.contract_patch_plan_file is not None and args.anchor_context_file is not None:
            try:
                contract_patch_plan_arg = json.loads(
                    args.contract_patch_plan_file.read_text(encoding="utf-8")
                )
                anchor_context_arg = json.loads(args.anchor_context_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(
                    json.dumps(
                        {"status": "environment_failure", "reason_code": "malformed_json", "error": str(exc)}
                    )
                )
                sys.exit(EXIT_ENVIRONMENT_FAILURE)
        receipt = consume_authority_transport(
            router_receipt_path=args.consume_authority_transport,
            issue_number=args.issue_number,
            repo=args.repo,
            invocation_id=args.invocation_id or "",
            git_head_sha=git_head_sha,
            repo_root=_repo_root,
            contract_patch_plan=contract_patch_plan_arg,
            anchor_context=anchor_context_arg,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        sys.exit(0 if receipt.get("status") == "ok" else EXIT_ENVIRONMENT_FAILURE)

    # --- argparse input validation (blocked / exit 2 on contract violation) ---
    input_errors: list[str] = []

    if args.issue_number is not None and args.issue_number <= 0:
        input_errors.append(f"--issue-number must be a positive int, got {args.issue_number}")

    if not _REPO_PATTERN.match(args.repo):
        input_errors.append(f"--repo must match ^[^/]+/[^/]+$, got {args.repo!r}")

    for url in args.anchor_comment_urls:
        if not url.startswith(_GITHUB_URL_PREFIX):
            input_errors.append(f"--anchor-comment-url must start with {_GITHUB_URL_PREFIX!r}, got {url!r}")

    # Issue #2689 P0-1 fix_delta: --mutation-category / --owner-user-id /
    # --preview-binding-file are an all-or-none atomic group -- any partial
    # combination is a contract violation (fail-closed exit 2), mirroring
    # the exact-grammar posture `skill_runtime_command_policy.py`'s
    # production transport enforces for this same triple.
    _mutation_gate_fields_present = (
        args.mutation_category is not None,
        args.owner_user_id is not None,
        args.preview_binding_file is not None,
    )
    if any(_mutation_gate_fields_present) and not all(_mutation_gate_fields_present):
        input_errors.append(
            "--mutation-category/--owner-user-id/--preview-binding-file must be "
            "supplied together or not at all"
        )
    if args.owner_user_id is not None and args.owner_user_id <= 0:
        input_errors.append(f"--owner-user-id must be a positive int, got {args.owner_user_id}")

    if input_errors:
        # Build minimal blocked result for argparse validation failure
        _repo_root = _find_repo_root()
        err_detail = "; ".join(input_errors)
        result = _build_result(
            status="blocked",
            issue_number=args.issue_number or 0,
            repo=args.repo or "",
            planner_exit_code=None,
            planner_fail_closed=None,
            next_action="human_judgment_required",
            must_read=[],
            do_not_read=[],
            commands=[],
            blockers=[BLOCKER_INVALID_ARGS, f"arg_errors: {err_detail}"],
            planner_fail_closed_reason_codes=[],
            required_sections=[],
            required_contract_keys=[],
            rewrite_constraints=None,
            artifacts={},
            hashes={},
        )
        print(_build_compact_stdout(result))
        sys.exit(EXIT_BLOCKED)

    cli_known_context = None
    if args.human_context_comment_urls or args.agent_report_comment_urls:
        cli_known_context = {
            _HUMAN_CONTEXT_COMMENT_URLS_FIELD: args.human_context_comment_urls,
            _AGENT_REPORT_COMMENT_URLS_FIELD: args.agent_report_comment_urls,
        }

    # Issue #2689 P0-1 fix_delta: materialize `known_context["mutation_category"]`
    # / `known_context["owner_reaction_context"]` from the validated CLI
    # triple above -- this is the ONLY place in `main()` that populates
    # these two keys; `run_preflight()` -> `_classify_heavy_mutation_gate_
    # with_fresh_owner_reaction()` reads them unchanged (#2689 AC4).
    if args.mutation_category is not None:
        cli_known_context = dict(cli_known_context) if cli_known_context else {}
        cli_known_context["mutation_category"] = args.mutation_category
        cli_known_context["owner_reaction_context"] = {
            "owner_user_id": args.owner_user_id,
            "preview_binding_file": args.preview_binding_file,
        }

    _, exit_code = run_preflight(
        issue_number=args.issue_number,
        repo=args.repo,
        anchor_comment_urls=args.anchor_comment_urls,
        fixture_path=args.fixture,
        known_context=cli_known_context,
        consume_contract_patch_plan=args.consume_contract_patch_plan,
        investigation_evidence_transport_path=args.investigation_evidence_transport_path,
        investigation_evidence_primary_root=args.investigation_evidence_primary_root,
        enable_main_drift_live_readback=args.enable_main_drift_live_readback,
        evidence_cache_enabled=args.evidence_cache_enabled,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
