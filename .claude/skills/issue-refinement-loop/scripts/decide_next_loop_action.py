#!/usr/bin/env python3
"""
decide_next_loop_action.py

Deterministic routing script for the issue-refinement-loop orchestrator.
Reads LOOP_STATE_V1 (from file or stdin JSON) and a compact review result,
then emits the next action to stdout.

This script is READ-ONLY with respect to loop state — it never mutates the
state file. All mutation is the orchestrator's responsibility.

#1873 (bounded review loops): this router was reduced to a bounded
verdict/iteration/max_iterations decision. The Replay Arbitration (Step 2a),
ISSUE_REFINEMENT_PHASE_STATE_V1 phase-gate, and ISSUE_EXECUTION_DECISION_V1
self-validation that previously wrapped this router were removed — the
orchestrator now trusts the reviewer VERDICT directly (see
`compact_review_result.py` / `issue-reviewer` SubAgent) instead of
independently re-deriving it via `reviewer_claim_replay.py`.

Input:
  --loop-state-file <path>      Path to LOOP_STATE_V1 JSON file
  --loop-state-json <json>      Inline LOOP_STATE_V1 JSON (alternative to file)
  --review-result-verdict <v>   One of: approve | needs-fix | null  (optional;
                                when omitted, loop_state.last_verdict is used)
  --max-iterations <N>          Override max_iterations from state (default: 3)

Output (stdout, budget < 2000 bytes):
  STATUS: pass | warn | human_escalation | inconsistent_state
  NEXT_ACTION: continue_to_step_4 | proceed_to_step_4_5 | human_escalation |
               terminate | proceed_with_contract_update
  BLOCKERS: (optional) blocker codes

Exit codes:
  0  pass      — NEXT_ACTION is actionable
  1  warn      — NEXT_ACTION is actionable but has notes
  2  human_escalation — stop and report to human
  3  inconsistent_state — state is corrupt or contradictory

Priority: inconsistent_state (3) > human_escalation (2) > warn (1) > pass (0).

Verdict resolution:
  When --review-result-verdict is omitted (or passed as "null"/""),
  the router uses loop_state.last_verdict as the single source of truth.
  When both --review-result-verdict and loop_state.last_verdict are non-null
  and differ, the router exits with inconsistent_state (exit 3).

Core decision (verdict / iteration / max_iterations only):
  VERDICT == approve                                -> terminate(approve)
  VERDICT == needs-fix and iteration < max_iterations -> continue(rewrite)
  otherwise (blocked, or iteration >= max_iterations) -> terminate(human_review_required)

scope_signal_guard hard-stop and the #1090/#1323 scope_delta_authority
non-destructive branch (owned by the ANCHOR_SCOPE_REFRAME_V1 lane, #1869)
remain intact — they are evaluated with higher priority than the verdict
routing above and are NOT part of the #1873 simplification scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

# #2053 P1 fix-delta (iteration 3, OWNER PR review): actually enforce
# SCOPE_DELTA_ROUTER_RECEIPT_V1 via _validate_with_schema() in the router,
# not just the manual field-by-field construction in generate_router_receipt().
try:
    import jsonschema as _jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - jsonschema is a declared dependency
    _JSONSCHEMA_AVAILABLE = False

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def _load_router_schema(schema_filename: str) -> "dict | None":
    schema_path = _SCHEMAS_DIR / schema_filename
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _validate_router_receipt_with_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    if not _JSONSCHEMA_AVAILABLE:
        return True, []
    try:
        validator_cls = _jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator = validator_cls(schema, format_checker=validator_cls.FORMAT_CHECKER)
        errors = sorted(validator.iter_errors(data), key=lambda exc: list(exc.path))
        if errors:
            return False, [f"schema_validation_error: {errors[0].message}"]
        return True, []
    except _jsonschema.ValidationError as exc:
        return False, [f"schema_validation_error: {exc.message}"]
    except Exception as exc:
        return False, [f"schema_validation_unexpected: {exc}"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "loop_state/v1"

# Verdict constants
VERDICT_APPROVE = "approve"
VERDICT_NEEDS_FIX = "needs-fix"
VERDICT_NULL = None

# Next action constants
ACTION_CONTINUE_TO_STEP_4 = "continue_to_step_4"
ACTION_PROCEED_TO_STEP_4_5 = "proceed_to_step_4_5"
ACTION_HUMAN_ESCALATION = "human_escalation"
ACTION_TERMINATE = "terminate"
ACTION_PROCEED_WITH_CONTRACT_UPDATE = "proceed_with_contract_update"

# Status constants
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_HUMAN_ESCALATION = "human_escalation"
STATUS_INCONSISTENT_STATE = "inconsistent_state"

# Exit codes
EXIT_PASS = 0
EXIT_WARN = 1
EXIT_HUMAN_ESCALATION = 2
EXIT_INCONSISTENT_STATE = 3
EXIT_ENVIRONMENT_FAILURE = 4

DEFAULT_MAX_ITERATIONS = 3

# #2053 AC7/AC8: router (`decide.run`) authority transport verification.
# STATUS_ENVIRONMENT_FAILURE is distinct from STATUS_INCONSISTENT_STATE --
# it means the *transport* between producer and router is untrustworthy
# (missing/malformed/digest-mismatch/source-mismatch sidecar), not that the
# loop state itself is corrupt. It never silently downgrades to a legacy
# route when authority_expected=true (AC8).
STATUS_ENVIRONMENT_FAILURE = "environment_failure"
ROUTER_RECEIPT_SCHEMA_VERSION = "SCOPE_DELTA_ROUTER_RECEIPT_V1"
CANONICALIZATION_ID = "loop-protocol-json-c14n-v1"


# ---------------------------------------------------------------------------
# Minimal structural validation
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    """`loop-protocol-json-c14n-v1`: sorted keys, compact separators, UTF-8,
    no NaN/Infinity. Producer (run_refinement_preflight.py) and router
    (this script) must hash identical bytes for digest binding to be
    meaningful (#2053).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _atomic_write_json_with_readback(path: Path, data: dict) -> tuple[bool, str | None]:
    """flush -> fsync -> os.replace, then read back and verify the digest
    (#2053 AC10 pattern, shared with run_refinement_preflight.py's producer
    / consumer telemetry writers). Returns (ok, error_reason).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}"
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False, f"write_failure:{type(exc).__name__}:{exc}"

    try:
        readback = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"write_failure:{type(exc).__name__}:{exc}"

    if _sha256(_canonical_json(readback)) != _sha256(_canonical_json(data)):
        return False, "write_failure:readback_digest_mismatch"
    return True, None


def generate_router_receipt(
    *,
    transport_manifest_path: "str | None",
    issue_number: "int | None",
    invocation_id: "str | None",
    git_head_sha: "str | None",
    authority_expected: bool,
    repo: "str | None" = None,
    repo_root: "Path | None" = None,
) -> "dict | None":
    """#2053 AC7/AC8: router-side verification of a SCOPE_DELTA_AUTHORITY_TRANSPORT_V1
    manifest, producing an immutable SCOPE_DELTA_ROUTER_RECEIPT_V1.

    Returns None when `authority_expected` is False AND no manifest path was
    supplied -- the ordinary route with no authority transport in play,
    unaffected by this check (not a fail-closed condition).

    When `authority_expected` is True, any of missing/malformed/digest
    mismatch/source (issue/git-head/invocation) mismatch is reported with
    `status: "environment_failure"` -- it is never silently downgraded to a
    legacy route (AC8).
    """
    if not authority_expected and not transport_manifest_path:
        return None

    generated_at_source = _generate_router_receipt_now_iso()
    base = {
        "schema_version": ROUTER_RECEIPT_SCHEMA_VERSION,
        "invocation_id": invocation_id or "unknown",
        "issue_number": issue_number if isinstance(issue_number, int) else 0,
        "git_head_sha": git_head_sha or "unknown",
        "generated_at": generated_at_source,
        "transport_manifest_path": transport_manifest_path,
        "transport_payload_sha256": None,
        "recomputed_payload_sha256": None,
    }

    def _fail(reason_code: str) -> dict:
        receipt = dict(base)
        receipt["status"] = "environment_failure"
        receipt["reason_code"] = reason_code
        return receipt

    if not transport_manifest_path:
        return _fail("missing_file")

    manifest_path = Path(transport_manifest_path)
    if not manifest_path.exists():
        return _fail("missing_file")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail("malformed_json")

    if not isinstance(manifest, dict) or manifest.get("schema_version") != "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1":
        return _fail("malformed_json")

    manifest_payload_sha256 = manifest.get("payload_sha256")
    base["transport_payload_sha256"] = manifest_payload_sha256

    if issue_number is not None and manifest.get("issue_number") != issue_number:
        return _fail("wrong_issue")
    if git_head_sha is not None and manifest.get("git_head_sha") != git_head_sha:
        return _fail("wrong_git_head")
    if invocation_id is not None and manifest.get("invocation_id") != invocation_id:
        return _fail("wrong_invocation_id")
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): the router
    # previously accepted no `repo` argument at all, so a wrong-repo
    # manifest (same issue number, different repo) would pass through
    # unnoticed. Only enforced when the caller supplies an expected repo,
    # mirroring the issue_number/git_head_sha/invocation_id checks above.
    if repo is not None and manifest.get("repo") != repo:
        return _fail("wrong_repo")

    recomputed = _sha256(_canonical_json(manifest.get("payload")))
    base["recomputed_payload_sha256"] = recomputed
    if recomputed != manifest_payload_sha256:
        return _fail("digest_mismatch")

    receipt = dict(base)
    receipt["status"] = "ok"
    receipt["reason_code"] = None

    receipt_schema = _load_router_schema("scope_delta_router_receipt_v1.schema.json")
    if receipt_schema is not None:
        valid, _schema_errors = _validate_router_receipt_with_schema(receipt, receipt_schema)
        if not valid:
            return _fail("schema_invalid")

    if repo_root is not None and isinstance(issue_number, int) and invocation_id:
        receipt_dir = (
            repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
            / "authority-transport" / invocation_id
        )
        receipt_path = receipt_dir / "scope_delta_router_receipt_v1.json"
        ok, error = _atomic_write_json_with_readback(receipt_path, receipt)
        if not ok:
            failed = dict(base)
            failed["status"] = "environment_failure"
            failed["reason_code"] = "write_failure"
            return failed

    return receipt


def _find_repo_root_for_receipt() -> "Path | None":
    """Best-effort .git root walk-up for writing the router receipt
    artifact. Returns None (receipt not persisted to disk, verification
    logic unaffected) if no .git root is found -- callers in tests may run
    from a tmp_path fixture directory tree.
    """
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _generate_router_receipt_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def validate_loop_state(data: Any) -> tuple[bool, str]:
    """
    Validate the minimal shape this router depends on.

    #1873: the router no longer validates the full LOOP_STATE_V1 JSON Schema
    (schemas/loop_state.schema.json was removed along with
    build_loop_state.py). It only checks the fields it actually reads:
    `iteration` and `max_iterations` (both optional, must be int if present).

    Returns (valid, error_message). Never raises for ordinary failures.
    """
    if not isinstance(data, dict):
        return False, "loop state must be a JSON object"

    iteration = data.get("iteration", 0)
    if not isinstance(iteration, int) or isinstance(iteration, bool):
        return False, f"iteration must be an int, got {type(iteration).__name__}"

    max_iterations = data.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        return False, f"max_iterations must be an int, got {type(max_iterations).__name__}"

    scope_signal = data.get("scope_signal_guard", {})
    if scope_signal is not None and not isinstance(scope_signal, dict):
        return False, "scope_signal_guard must be an object when present"

    return True, ""


# ---------------------------------------------------------------------------
# Core routing logic (pure function)
# ---------------------------------------------------------------------------


def decide_next_action(
    loop_state: dict[str, Any],
    review_verdict: Optional[str],
    max_iterations_override: Optional[int] = None,
    scope_signal_guard_decision_v2: Optional[dict[str, Any]] = None,
) -> tuple[str, str, list[str], list[str], Optional[str]]:
    """
    Determine the next action for the refinement loop.

    Args:
        loop_state: Validated LOOP_STATE_V1-shaped dict (read-only).
        review_verdict: "approve" | "needs-fix" | None
        max_iterations_override: If provided, overrides loop_state["max_iterations"].
        scope_signal_guard_decision_v2: Optional SCOPE_SIGNAL_GUARD_DECISION_V2
            sidecar (#1090/#1323). NOT part of LOOP_STATE_V1 — passed as a
            separate argument, same pattern as before. When
            scope_signal_guard_decision_v2.scope_delta_authority.route.action
            == "contract_update_required" (the SCOPE_DELTA_AUTHORITY_V1 nested
            route emitted by classify_scope_delta_authority() in
            scope_signal_delta.py), this router returns
            NEXT_ACTION: proceed_with_contract_update without touching
            termination_reason (loop stays open, Issue contract update
            happens out-of-band, then refinement re-runs).

    Returns:
        (status, next_action, commands, blockers, termination_cause_hint)

    Priority order:
        1. inconsistent_state — corrupt/contradictory state fields
        2. proceed_with_contract_update —
           scope_signal_guard_decision_v2.scope_delta_authority.route.action
           == contract_update_required (#1323, non-destructive branch)
        3. scope_signal_guard hard stop
        4. max_iterations exceeded
        5. routing on verdict (bounded #1873 decision: approve / needs-fix / other)
    """
    iteration: int = loop_state.get("iteration", 0)
    max_iterations: int = (
        max_iterations_override
        if max_iterations_override is not None
        else loop_state.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    )
    termination_reason = loop_state.get("termination_reason")
    scope_signal = loop_state.get("scope_signal_guard", {}) or {}
    blockers: list[str] = []
    _commands: list[str] = []

    # --- Priority 1: inconsistent_state detection ---
    if iteration < 0:
        return (
            STATUS_INCONSISTENT_STATE,
            ACTION_HUMAN_ESCALATION,
            [],
            ["iteration_negative"],
            None,
        )
    if max_iterations < 1:
        return (
            STATUS_INCONSISTENT_STATE,
            ACTION_HUMAN_ESCALATION,
            [],
            ["max_iterations_below_1"],
            None,
        )

    # --- Already terminated ---
    if termination_reason is not None:
        return (
            STATUS_PASS,
            ACTION_TERMINATE,
            [],
            [],
            None,
        )

    # --- Priority 2 (#1323): explicit human-review contract-update directive.
    # Non-destructive: termination_reason is left untouched (loop keeps
    # running); this only redirects the immediate next step toward a
    # contract update + refinement re-run instead of human_escalation.
    _scope_delta_authority = (
        scope_signal_guard_decision_v2.get("scope_delta_authority")
        if isinstance(scope_signal_guard_decision_v2, dict)
        else None
    )
    _authority_route_action = (
        _scope_delta_authority.get("route", {}).get("action")
        if isinstance(_scope_delta_authority, dict)
        else None
    )
    if _authority_route_action == "contract_update_required":
        return (
            STATUS_PASS,
            ACTION_PROCEED_WITH_CONTRACT_UPDATE,
            [],
            [],
            None,
        )

    # --- Priority 3: scope signal guard hard stop ---
    if scope_signal.get("triggered") and not scope_signal.get(
        "excluded_by_anchor_reframe", False
    ):
        reason_code = scope_signal.get("reason_code")
        scope_blockers = ["scope_signal_guard_triggered"]
        if reason_code:
            scope_blockers.append(f"scope_signal_guard_reason_code:{reason_code}")
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            scope_blockers,
            "human_judgment_required",
        )

    # --- Priority 4: max_iterations exceeded ---
    if review_verdict == VERDICT_NEEDS_FIX and iteration + 1 >= max_iterations:
        blockers = ["max_iterations_exceeded"]
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            blockers,
            "max_iterations_exceeded",
        )

    # --- Priority 5: bounded verdict routing (#1873) ---
    if review_verdict == VERDICT_APPROVE:
        return (
            STATUS_PASS,
            ACTION_PROCEED_TO_STEP_4_5,
            [],
            [],
            None,
        )

    if review_verdict == VERDICT_NEEDS_FIX:
        # iteration + 1 < max_iterations is guaranteed here (else escalated above)
        return (
            STATUS_PASS,
            ACTION_CONTINUE_TO_STEP_4,
            [],
            [],
            None,
        )

    # verdict is null/blocked/unknown — terminate to human review
    blockers = [f"unknown_verdict:{review_verdict}"]
    return (
        STATUS_WARN,
        ACTION_HUMAN_ESCALATION,
        [],
        blockers,
        None,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_output(
    status: str,
    next_action: str,
    commands: list[str],
    blockers: list[str],
    termination_cause_hint: Optional[str] = None,
) -> str:
    """Format the stdout output (budget < 2000 bytes)."""
    lines = [
        f"STATUS: {status}",
        f"NEXT_ACTION: {next_action}",
    ]
    if termination_cause_hint is not None:
        lines.append(f"TERMINATION_CAUSE: {termination_cause_hint}")
    if commands:
        for cmd in commands:
            lines.append(f"COMMANDS: {cmd}")
    if blockers:
        for b in blockers:
            lines.append(f"BLOCKERS: {b}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decide next action for the issue-refinement-loop."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--loop-state-file",
        metavar="PATH",
        help="Path to LOOP_STATE_V1 JSON file.",
    )
    group.add_argument(
        "--loop-state-json",
        metavar="JSON",
        help="Inline LOOP_STATE_V1 JSON string.",
    )
    parser.add_argument(
        "--review-result-verdict",
        metavar="VERDICT",
        default=None,
        help=(
            "Review result verdict: approve | needs-fix | null. "
            "When omitted, loop_state.last_verdict is used as the single source of truth. "
            "When both are provided and non-null, they must agree (else inconsistent_state)."
        ),
    )
    parser.add_argument(
        "--reviewer-transport-result-file",
        metavar="PATH",
        default=None,
        help=(
            "#2054 parent-owned REVIEWER_TRANSPORT_RESULT_V1. A transport "
            "failure is routed as environment_failure; only an ok result may "
            "supply semantic_verdict."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        metavar="N",
        help="Override max_iterations from state (default: 3).",
    )
    sig_group = parser.add_mutually_exclusive_group()
    sig_group.add_argument(
        "--scope-signal-guard-decision-v2-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to a SCOPE_SIGNAL_GUARD_DECISION_V2 JSON sidecar (#1090/#1323). "
            "NOT part of LOOP_STATE_V1; when route == contract_update_required "
            "this router returns NEXT_ACTION: proceed_with_contract_update "
            "without touching termination_reason."
        ),
    )
    sig_group.add_argument(
        "--scope-signal-guard-decision-v2-json",
        metavar="JSON",
        default=None,
        help="Inline SCOPE_SIGNAL_GUARD_DECISION_V2 JSON string (alternative to the file form).",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        metavar="N",
        help="#2053: GitHub Issue number, used to verify a SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest.",
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        default=None,
        help="#2053 P1 fix-delta: owner/repo cross-checked against the manifest's own `repo` field "
        "(prevents same-issue-number/cross-repo spoofing).",
    )
    parser.add_argument(
        "--authority-transport-path",
        metavar="PATH",
        default=None,
        help="#2053: path to a SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest produced by run_refinement_preflight.py.",
    )
    parser.add_argument(
        "--authority-expected",
        action="store_true",
        help="#2053 AC8: when set, a missing/malformed/digest-mismatch/source-mismatch authority "
        "transport manifest is fail-closed to environment_failure instead of being silently skipped.",
    )
    parser.add_argument(
        "--invocation-id",
        metavar="ID",
        default=None,
        help="#2053: invocation identifier bound into the SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest.",
    )
    parser.add_argument(
        "--git-head-sha",
        metavar="SHA",
        default=None,
        help="#2053: current git HEAD sha, cross-checked against the manifest's git_head_sha.",
    )
    return parser.parse_args(argv)


def _load_scope_signal_guard_decision_v2(
    args: argparse.Namespace,
) -> tuple[Optional[dict[str, Any]], str]:
    """Load the optional SCOPE_SIGNAL_GUARD_DECISION_V2 sidecar (#1090/#1323).

    Returns (data, error_msg). error_msg is '' on success (including the
    "not provided" case, where data is None). Malformed input is a soft
    failure (warning only, sidecar treated as absent) -- this sidecar is
    additive/optional and must never fail-closed the whole router.
    """
    if getattr(args, "scope_signal_guard_decision_v2_file", None):
        path = Path(args.scope_signal_guard_decision_v2_file)
        if not path.exists():
            return None, f"scope_signal_guard_decision_v2 file not found: {path}"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return None, f"Invalid scope_signal_guard_decision_v2 file: {e}"
        if not isinstance(data, dict):
            return None, "scope_signal_guard_decision_v2 must be a JSON object"
        return data, ""

    if getattr(args, "scope_signal_guard_decision_v2_json", None):
        try:
            data = json.loads(args.scope_signal_guard_decision_v2_json)
        except json.JSONDecodeError as e:
            return None, f"Invalid inline scope_signal_guard_decision_v2 JSON: {e}"
        if not isinstance(data, dict):
            return None, "scope_signal_guard_decision_v2 must be a JSON object"
        return data, ""

    return None, ""


def _load_loop_state(args: argparse.Namespace) -> tuple[Optional[dict[str, Any]], str]:
    """Load loop state from file or inline JSON. Returns (data, error_msg)."""
    if args.loop_state_file:
        path = Path(args.loop_state_file)
        if not path.exists():
            return None, f"Loop state file not found: {path}"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON in loop state file: {e}"
        except OSError as e:
            return None, f"Cannot read loop state file: {e}"
        return data, ""

    if args.loop_state_json:
        try:
            data = json.loads(args.loop_state_json)
        except json.JSONDecodeError as e:
            return None, f"Invalid inline JSON: {e}"
        return data, ""

    # No source — try stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None, "No loop state provided (file, JSON, or stdin required)"
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON from stdin: {e}"
    return data, ""


def _load_reviewer_transport_result(path_value: str | None) -> tuple[Optional[dict[str, Any]], str]:
    """Load the parent result without accepting child compact/V1 fallback."""
    if path_value is None:
        return None, ""
    try:
        with open(path_value, "rb") as stream:
            from reviewer_transport import strict_json_loads

            data = strict_json_loads(stream.read())
    except (OSError, ValueError, ImportError) as exc:
        return None, f"reviewer_transport_result_invalid:{type(exc).__name__}"
    if not isinstance(data, dict) or data.get("schema") != "REVIEWER_TRANSPORT_RESULT_V1":
        return None, "reviewer_transport_result_invalid:schema"
    status = data.get("transport_status")
    verdict = data.get("semantic_verdict")
    if status == STATUS_ENVIRONMENT_FAILURE and verdict is None:
        return data, ""
    if status == "ok" and verdict in {"approve", "needs-fix"}:
        return data, ""
    return None, "reviewer_transport_result_invalid:status_or_verdict"


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # #2053 AC7/AC8: verify authority transport BEFORE loop-state loading --
    # this is a distinct producer/router transport-trust check, not a
    # loop-state consistency check, and must never be masked by (or masked
    # as) inconsistent_state.
    router_receipt = generate_router_receipt(
        transport_manifest_path=args.authority_transport_path,
        issue_number=args.issue_number,
        invocation_id=args.invocation_id,
        git_head_sha=args.git_head_sha,
        authority_expected=args.authority_expected,
        repo=args.repo,
        repo_root=_find_repo_root_for_receipt(),
    )
    if router_receipt is not None and router_receipt.get("status") == STATUS_ENVIRONMENT_FAILURE:
        print(f"STATUS: {STATUS_ENVIRONMENT_FAILURE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: authority_transport_environment_failure:{router_receipt.get('reason_code')}")
        sys.exit(EXIT_ENVIRONMENT_FAILURE)

    transport_result, transport_error = _load_reviewer_transport_result(
        args.reviewer_transport_result_file
    )
    if transport_error:
        print(f"STATUS: {STATUS_ENVIRONMENT_FAILURE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {transport_error}")
        sys.exit(EXIT_ENVIRONMENT_FAILURE)
    if transport_result is not None and transport_result["transport_status"] == STATUS_ENVIRONMENT_FAILURE:
        print(f"STATUS: {STATUS_ENVIRONMENT_FAILURE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print("BLOCKERS: reviewer_transport_environment_failure")
        sys.exit(EXIT_ENVIRONMENT_FAILURE)

    # Load loop state
    loop_state, load_error = _load_loop_state(args)
    if load_error or loop_state is None:
        msg = load_error or "No loop state data"
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {msg}")
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Validate minimal shape (#1873: no full JSON Schema dependency)
    valid, error_msg = validate_loop_state(loop_state)
    if not valid:
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {error_msg}")
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Parse CLI verdict (may be absent / "null" / "")
    raw_verdict = (
        transport_result["semantic_verdict"]
        if transport_result is not None
        else args.review_result_verdict
    )
    if raw_verdict in (None, "null", ""):
        cli_verdict: Optional[str] = None
    else:
        cli_verdict = raw_verdict

    # Resolve single source of truth for verdict:
    # - If CLI verdict is absent, use loop_state.last_verdict.
    # - If CLI verdict is present and loop_state.last_verdict is also present
    #   and they differ → inconsistent_state.
    state_last_verdict = loop_state.get("last_verdict")

    if cli_verdict is None:
        # Use loop_state.last_verdict as the authoritative source.
        verdict: Optional[str] = state_last_verdict
    elif state_last_verdict is not None and cli_verdict != state_last_verdict:
        # Both non-null and conflicting.
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(
            f"BLOCKERS: last_verdict_conflict:"
            f" state={state_last_verdict!r} cli={cli_verdict!r}"
        )
        sys.exit(EXIT_INCONSISTENT_STATE)
    else:
        verdict = cli_verdict

    # Load optional SCOPE_SIGNAL_GUARD_DECISION_V2 sidecar (#1090/#1323).
    # Soft-fail: a malformed/missing sidecar never blocks the router; it is
    # simply treated as absent (BLOCKERS records the parse warning).
    scope_signal_guard_decision_v2, sidecar_load_error = _load_scope_signal_guard_decision_v2(args)
    sidecar_warning: list[str] = [sidecar_load_error] if sidecar_load_error else []

    # Compute next action
    status, next_action, commands, blockers, termination_cause_hint = decide_next_action(
        loop_state=loop_state,
        review_verdict=verdict,
        max_iterations_override=args.max_iterations,
        scope_signal_guard_decision_v2=scope_signal_guard_decision_v2,
    )
    blockers = list(blockers) + sidecar_warning

    # Emit output
    print(_format_output(status, next_action, commands, blockers, termination_cause_hint))

    # Exit with appropriate code
    exit_map = {
        STATUS_PASS: EXIT_PASS,
        STATUS_WARN: EXIT_WARN,
        STATUS_HUMAN_ESCALATION: EXIT_HUMAN_ESCALATION,
        STATUS_INCONSISTENT_STATE: EXIT_INCONSISTENT_STATE,
    }
    sys.exit(exit_map.get(status, EXIT_INCONSISTENT_STATE))


if __name__ == "__main__":
    main()
