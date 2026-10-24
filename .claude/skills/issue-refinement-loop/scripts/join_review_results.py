#!/usr/bin/env python3
"""Pure joiner for deterministic + semantic Step 2 review results (Issue #2296).

``join_review_results()`` is a pure function: it never invokes any child
process, never calls GitHub, and never launches ``issue-design-reviewer``.
It only combines already-computed inputs:

- ``deterministic_verdict``: ``approve`` | ``needs-fix`` (from the existing
  Step 2 root_review_pipeline / ISSUE_REVIEW_RESULT_COMPACT_V2 pipeline --
  unchanged by this Issue)
- ``semantic_assessment``: ``clear`` | ``findings`` | ``not_required`` (the
  latter when ``semantic_review_applicable=False``)
- ``transport_status``: ``ok`` | ``missing`` | ``stale`` | ``error`` |
  ``not_required``
- ``findings``: the raw finding list (each optionally carrying a
  post-hoc ``owner_disposition`` recorded by the owner/orchestrator, never
  by the model itself -- see ``semantic_review_transport.py``)

into a single ``effective_verdict`` (``approve`` | ``needs-fix`` | ``retry``
| ``human_judgment_required``) plus a ``warnings`` list.

#2296 fix_delta iteration 6 (OWNER adversarial review, P0-3): the decision
is now a total function computed from ``findings`` FIRST, not from the
``semantic_assessment`` label alone -- a malformed/inconsistent producer
output (e.g. ``assessment: clear`` but a non-empty findings list containing
a blocker) can no longer fail open into ``approve``. Precedence:

1. ``deterministic_verdict != "approve"`` -> ``needs-fix`` (deterministic
   always wins; semantic gate only applies on the approve path).
2. unknown/invalid ``semantic_assessment`` or ``transport_status`` enum
   value -> treated as a transport failure (fail-closed), routed through
   the same retry / required / best_effort policy as (5).
3. any OPEN (no valid owner_disposition, see below) ``blocker``/``high``
   finding -> ``needs-fix``, regardless of what ``semantic_assessment``
   claims, and regardless of ``transport_status`` (a genuine blocking
   finding is never silently dropped).
4. ``semantic_assessment == "not_required"`` -> ``approve``.
5. ``transport_status`` in (``missing``, ``stale``, ``error``) (a *known*
   transport failure) and no open blocking finding was found in (3):
   - ``transport_policy == "required"`` -> ``human_judgment_required``
   - ``transport_policy == "best_effort"`` and
     ``retry_already_attempted is False`` -> ``retry`` (the caller must
     re-invoke the transport exactly once and call this function again
     with ``retry_already_attempted=True``; ``decide_next_loop_action.py``
     never observes this intermediate ``retry`` state)
   - ``transport_policy == "best_effort"`` and
     ``retry_already_attempted is True`` -> ``approve`` + a durable
     ``semantic_review_unavailable: true`` marker and an explicit
     ``SEMANTIC_REVIEW_UNAVAILABLE`` warning string (P0-3/P1-5: this must
     survive into the terminal/final report, not just an intermediate
     field that gets discarded)
6. otherwise (``transport_status == "ok"``, no open blocking finding):
   ``approve``, with a warning if any non-blocking findings remain
   (medium/low severity, or blocker/high with a valid owner_disposition).

``owner_disposition`` only downgrades a finding's severity when it is
fully valid (P1-1): ``recorded_by == "owner"`` AND ``status`` in
(``accepted``, ``deferred``) AND ``reason`` is a non-empty string. A
forged/incomplete ``owner_disposition`` (e.g. missing ``recorded_by`` or
empty ``reason``) never neutralizes a blocker/high finding.

When ``effective_verdict == "needs-fix"`` is driven by an open semantic
finding (not by a deterministic ``needs-fix``), the result additionally
carries ``rewrite_lane: "semantic"`` and a
``semantic_rewrite_constraints`` payload (``SEMANTIC_REWRITE_CONSTRAINTS_V1``,
see ``.claude/agents/issue-editor.md``) so Step 4 forwards it to
``issue-editor`` as-is, without reconstructing it (P0-4).

``decide_next_loop_action.py`` is NOT called from here and is not modified
by this Issue -- ``effective_verdict`` values ``approve`` / ``needs-fix`` /
``human_judgment_required`` are handed to the EXISTING Step 2 routing table
unchanged (approve -> Step 4.5 / needs-fix -> Step 4 / human_judgment_required
-> Step 5). ``retry`` is consumed entirely inside the Step 2.5 orchestration
loop (re-invoke transport once, call this function again) and never reaches
``decide_next_loop_action.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SCHEMA = "JOIN_REVIEW_RESULT_V1"

_ACCEPTED_LIKE_STATUSES = {"accepted", "deferred"}
_KNOWN_TRANSPORT_FAILURE_STATUSES = {"missing", "stale", "error"}
_VALID_SEMANTIC_ASSESSMENTS = {"not_required", "clear", "findings"}
_VALID_TRANSPORT_STATUSES = {"ok", "not_required"} | _KNOWN_TRANSPORT_FAILURE_STATUSES

DEFAULT_TRANSPORT_POLICY = "best_effort"
DEFAULT_FINDING_POLICY = "route_high_open_to_rewrite"

_VALID_TRANSPORT_POLICIES = {"best_effort", "required"}
_VALID_FINDING_POLICIES = {"route_high_open_to_rewrite"}

SEMANTIC_REWRITE_CONSTRAINTS_SCHEMA_VERSION = "SEMANTIC_REWRITE_CONSTRAINTS_V1"
DEFAULT_MAX_REWRITE_ATTEMPTS = 2
DEFAULT_NO_PROGRESS_ROUTE = "human_judgment_required"

SEMANTIC_REVIEW_UNAVAILABLE_WARNING = "SEMANTIC_REVIEW_UNAVAILABLE"


def _valid_owner_disposition(disposition: Any) -> bool:
    """P1-1: only a fully-formed, owner-authored disposition neutralizes a
    blocker/high finding -- ``recorded_by == "owner"`` AND ``status`` in
    (accepted, deferred) AND a non-empty ``reason`` string. A forged or
    incomplete disposition (missing ``recorded_by``, missing/empty
    ``reason``) is NOT sufficient."""
    if not isinstance(disposition, dict):
        return False
    if disposition.get("recorded_by") != "owner":
        return False
    if disposition.get("status") not in _ACCEPTED_LIKE_STATUSES:
        return False
    reason = disposition.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return False
    return True


def _finding_is_open_blocking(finding: "dict[str, Any]") -> bool:
    severity = finding.get("severity")
    if severity not in ("blocker", "high"):
        return False
    if _valid_owner_disposition(finding.get("owner_disposition")):
        return False
    return True


def _semantic_rewrite_constraints(
    *,
    open_findings: "list[dict[str, Any]]",
    source_artifact: "str | None",
    checked_body_sha256: "str | None",
) -> "dict[str, Any]":
    return {
        "schema_version": SEMANTIC_REWRITE_CONSTRAINTS_SCHEMA_VERSION,
        "source_artifact": source_artifact,
        "checked_body_sha256": checked_body_sha256,
        "findings": open_findings,
        "max_rewrite_attempts": DEFAULT_MAX_REWRITE_ATTEMPTS,
        "no_progress_route": DEFAULT_NO_PROGRESS_ROUTE,
    }


def _result(
    *,
    effective_verdict: str,
    warnings: "list[str]",
    transport_policy: str,
    finding_policy: str,
    semantic_review_unavailable: bool = False,
    rewrite_lane: "str | None" = None,
    semantic_rewrite_constraints: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    out: "dict[str, Any]" = {
        "schema": SCHEMA,
        "effective_verdict": effective_verdict,
        "warnings": warnings,
        "transport_policy": transport_policy,
        "finding_policy": finding_policy,
        "semantic_review_unavailable": semantic_review_unavailable,
    }
    if rewrite_lane is not None:
        out["rewrite_lane"] = rewrite_lane
    if semantic_rewrite_constraints is not None:
        out["semantic_rewrite_constraints"] = semantic_rewrite_constraints
    return out


def join_review_results(
    *,
    deterministic_verdict: str,
    semantic_assessment: str = "not_required",
    transport_status: str = "not_required",
    findings: "list[dict[str, Any]] | None" = None,
    transport_policy: str = DEFAULT_TRANSPORT_POLICY,
    finding_policy: str = DEFAULT_FINDING_POLICY,
    retry_already_attempted: bool = False,
    source_artifact: "str | None" = None,
    checked_body_sha256: "str | None" = None,
) -> "dict[str, Any]":
    findings = findings or []
    warnings: "list[str]" = []

    if transport_policy not in _VALID_TRANSPORT_POLICIES:
        raise ValueError(f"unknown transport_policy: {transport_policy!r}")
    if finding_policy not in _VALID_FINDING_POLICIES:
        raise ValueError(f"unknown finding_policy: {finding_policy!r}")

    # (1) deterministic needs-fix always wins; semantic gate only applies to
    # the approve path (Design Decision Note).
    if deterministic_verdict != "approve":
        return _result(
            effective_verdict="needs-fix",
            warnings=warnings,
            transport_policy=transport_policy,
            finding_policy=finding_policy,
        )

    # (2) unknown/invalid enum values are never silently treated as ok/clear
    # -- normalize them to the same fail-closed "known transport failure"
    # handling as an explicit missing/stale/error transport_status.
    assessment_invalid = semantic_assessment not in _VALID_SEMANTIC_ASSESSMENTS
    transport_status_invalid = transport_status not in _VALID_TRANSPORT_STATUSES
    effective_transport_status = transport_status
    if assessment_invalid or transport_status_invalid:
        warnings.append(
            f"invalid semantic_assessment={semantic_assessment!r} or "
            f"transport_status={transport_status!r}: treated as a transport "
            "failure (fail-closed)"
        )
        effective_transport_status = "error"

    # (3) any OPEN blocker/high finding wins over the assessment label and
    # over transport_status -- a genuine blocking finding must never be
    # silently dropped just because the producer's own summary label or
    # transport status disagrees with the finding list it also emitted.
    open_blocking = [f for f in findings if _finding_is_open_blocking(f)]
    if open_blocking:
        warnings.append(
            f"{len(open_blocking)} open blocker/high semantic finding(s) route to rewrite"
        )
        return _result(
            effective_verdict="needs-fix",
            warnings=warnings,
            transport_policy=transport_policy,
            finding_policy=finding_policy,
            rewrite_lane="semantic",
            semantic_rewrite_constraints=_semantic_rewrite_constraints(
                open_findings=open_blocking,
                source_artifact=source_artifact,
                checked_body_sha256=checked_body_sha256,
            ),
        )

    # (4) semantic review lane was never applicable.
    if not assessment_invalid and semantic_assessment == "not_required":
        return _result(
            effective_verdict="approve",
            warnings=warnings,
            transport_policy=transport_policy,
            finding_policy=finding_policy,
        )

    # (5) known transport failure (or normalized-to-error invalid input),
    # and no open blocking finding was found above.
    if effective_transport_status in _KNOWN_TRANSPORT_FAILURE_STATUSES:
        if transport_policy == "required":
            warnings.append(f"semantic review transport_status={transport_status}")
            return _result(
                effective_verdict="human_judgment_required",
                warnings=warnings,
                transport_policy=transport_policy,
                finding_policy=finding_policy,
            )
        if not retry_already_attempted:
            warnings.append(
                f"semantic review transport_status={transport_status} "
                "(best_effort, first failure -- retrying once)"
            )
            return _result(
                effective_verdict="retry",
                warnings=warnings,
                transport_policy=transport_policy,
                finding_policy=finding_policy,
            )
        warnings.append(
            f"{SEMANTIC_REVIEW_UNAVAILABLE_WARNING}: semantic review transport_status="
            f"{transport_status} (best_effort, retry already attempted -- "
            "continuing without a semantic verdict)"
        )
        return _result(
            effective_verdict="approve",
            warnings=warnings,
            transport_policy=transport_policy,
            finding_policy=finding_policy,
            semantic_review_unavailable=True,
        )

    # (6) transport_status == "ok": approve, with a warning if any
    # non-blocking findings remain (medium/low, or owner-dispositioned).
    if findings:
        warnings.append(
            f"{len(findings)} semantic finding(s) recorded as warning "
            "(medium/low severity or owner-dispositioned)"
        )
    elif semantic_assessment == "findings":
        # Schema-level defense: assessment==findings with an empty findings
        # list should never reach the transport as a persisted artifact
        # (semantic_review_transport.py rejects it), but this joiner does
        # not trust that upstream invariant blindly either.
        warnings.append("assessment=findings but findings list empty; treated as clear")
    return _result(
        effective_verdict="approve",
        warnings=warnings,
        transport_policy=transport_policy,
        finding_policy=finding_policy,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file")
    parser.add_argument("--input-json")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.input_json:
        raw = json.loads(args.input_json)
    elif args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = json.load(sys.stdin)
    result = join_review_results(
        deterministic_verdict=raw["deterministic_verdict"],
        semantic_assessment=raw.get("semantic_assessment", "not_required"),
        transport_status=raw.get("transport_status", "not_required"),
        findings=raw.get("findings"),
        transport_policy=raw.get("transport_policy", DEFAULT_TRANSPORT_POLICY),
        finding_policy=raw.get("finding_policy", DEFAULT_FINDING_POLICY),
        retry_already_attempted=bool(raw.get("retry_already_attempted", False)),
        source_artifact=raw.get("source_artifact"),
        checked_body_sha256=raw.get("checked_body_sha256"),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
