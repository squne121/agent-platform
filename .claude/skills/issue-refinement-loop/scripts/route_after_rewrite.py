#!/usr/bin/env python3
"""
route_after_rewrite.py — Rewrite Loop Router Wrapper

Orchestrates the full rewrite-loop routing sequence in a single invocation:
  1. Fetch issue body from GitHub (or load from file for testing)
  2. Compute body_hash (sha256)
  3. Run check_issue_contract.py (checker) — stdout JSON only; stderr NOT merged
  4. Build a schema-valid LOOP_REWRITE_ROUTER_STATE_V1 from checker output
  5. Call decide_rewrite_route.py and print the RouteResult JSON to stdout

Design constraints (AC4):
  - Checker exit 0 (approve) and exit 1 (needs-fix) are BOTH routed normally.
    Exit 1 is NOT treated as an infrastructure failure.
  - checker stdout JSON is parsed exclusively; stderr is NOT merged into JSON.
  - Only LOOP_REWRITE_ROUTER_STATE_V1 schema-allowlist keys are set; no
    additional keys from the checker result are injected into the router state.
  - load_rewrite_router_state() / save_rewrite_router_state() are used for
    persistence; attempt counter is NEVER silently reset to 0.

Usage:
    uv run python3 route_after_rewrite.py \\
        --issue <number> \\
        --repo <owner/repo> \\
        --state-path <path/to/state.json> \\
        --max-rewrite-attempts <int>

    # File-based mode (for testing, bypasses gh):
    uv run python3 route_after_rewrite.py \\
        --file <path/to/issue_body.md> \\
        --state-path <path/to/state.json> \\
        --max-rewrite-attempts <int>

Exit codes:
    0 — RouteResult JSON written to stdout
    2 — Invalid input / schema error
    3 — Internal error (checker infrastructure failure, not checker exit 1)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPTS_DIR.parent
_CHECKER_SCRIPT = (
    _SKILL_ROOT.parent / "review-issue" / "scripts" / "check_issue_contract.py"
)
_ROUTER_SCRIPT = _SCRIPTS_DIR / "decide_rewrite_route.py"

# Import decide_rewrite_route helpers for persistence
sys.path.insert(0, str(_SCRIPTS_DIR))
from decide_rewrite_route import (  # noqa: E402
    LOOP_REWRITE_ROUTER_STATE_V1,
    SCHEMA_VERSION,
    ROUTE_PROCEED_TO_REVIEW,
    load_rewrite_router_state,
    save_rewrite_router_state,
    validate_state_dict,
    RewriteRouterStateError,
)


# ---------------------------------------------------------------------------
# LOOP_REWRITE_ROUTER_STATE_V1 schema allowlist
# These are the only keys that may appear in the router state dict.
# Do NOT add checker_result keys or any other extra fields here.
# ---------------------------------------------------------------------------

_STATE_ALLOWLIST = frozenset({
    "schema_version",
    "rewrite_attempt_count",
    "max_rewrite_attempts",
    "checker_exit_code",
    "checked_body_sha256",
    "fix_category",
    "rewrite_history",
    "occurrence_count",
    "missing_sections",
    "missing_contract_keys",
    "previous_checked_body_sha256",
    "previous_missing_sections",
    "previous_missing_contract_keys",
    "source_issue_body_sha256",
    "replay_safe",
    "source_body_reset",
    # B4: AC9/AC10 fields now persisted in JSON schema (loop_rewrite_router_state_v1.json updated)
    "rewrite_request_fingerprint",
    "previous_rewrite_request_fingerprints",
    "last_mutation_kind",
    "budget_debit",
})


# ---------------------------------------------------------------------------
# Body hash helper
# ---------------------------------------------------------------------------


def _sha256_of_body(body: str) -> str:
    """Compute sha256 hex digest of the issue body text (UTF-8 encoded)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# #2048 AC4: implementation_allowed gate
# ---------------------------------------------------------------------------
#
# After a rewrite mutation is applied (contract_update / issue_editor_required
# style body or title change), implementation must stay blocked until BOTH:
#   1. the mutated Issue body AND title have been read back from GitHub and
#      confirmed to match what was intended to be written (readback), and
#   2. a FRESH review/preflight run has completed against that readback
#      snapshot (a stale pre-mutation review/preflight result must not be
#      reused to unlock implementation).
# This is fail-closed: any missing/false confirmation keeps
# implementation_allowed False, and route != proceed_to_review always keeps
# it False regardless of readback state.


def compute_implementation_allowed(
    *,
    route: "str | None",
    body_readback_confirmed: bool,
    title_readback_confirmed: bool,
    fresh_review_preflight_completed: bool,
) -> bool:
    """
    AC4: implementation_allowed stays False until body/title readback is
    confirmed against the mutated Issue AND a fresh review/preflight has
    completed. Fail-closed: missing confirmation, a non-proceed_to_review
    route, or an unconfirmed readback/preflight all keep this False.
    """
    if route != ROUTE_PROCEED_TO_REVIEW:
        return False
    return bool(
        body_readback_confirmed
        and title_readback_confirmed
        and fresh_review_preflight_completed
    )


# ---------------------------------------------------------------------------
# Issue body fetcher
# ---------------------------------------------------------------------------


def _fetch_issue_body_from_github(issue_number: int, repo: str) -> str:
    """Fetch issue body text from GitHub using gh CLI."""
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "body",
        "--jq", ".body",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"ERROR: gh issue view failed (exit {result.returncode}): {result.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)
    return result.stdout.strip()


def _load_body_from_file(path: str) -> str:
    """Load issue body from a local file (for testing)."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"ERROR: Cannot read issue file {path}: {e}", file=sys.stderr)
        sys.exit(3)

    # Strip optional YAML-like front-matter (same convention as load_fixture_file)
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            content = content[end + 5:].strip()

    return content


# ---------------------------------------------------------------------------
# Checker invocation
# ---------------------------------------------------------------------------


def _run_checker(
    body: str,
    source_file_path: Optional[str] = None,
) -> tuple[int, dict]:
    """
    Run check_issue_contract.py on the given body text.

    Returns (exit_code, checker_json).

    AC4: checker exit 1 is NOT an infrastructure failure — it means needs-fix.
    Only exit codes other than 0/1 are treated as infrastructure failures.

    stderr is deliberately NOT captured into the JSON payload.

    When source_file_path is provided (--file mode), the original file is passed
    directly to the checker so that fixture front-matter (LABELS / TITLE) is
    preserved for issue_kind detection. In --issue mode (body from GitHub), a
    temp file is created from the fetched body text.
    """
    import tempfile

    if source_file_path is not None:
        # Pass original file directly — checker's load_fixture_file handles front-matter
        checker_file = source_file_path
        tmp_to_delete: Optional[str] = None
    else:
        # Write body to a temp file (no front-matter; GitHub-fetched body)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", encoding="utf-8", delete=False
        ) as tf:
            tf.write(body)
            checker_file = tf.name
            tmp_to_delete = tf.name

    try:
        proc = subprocess.run(
            [sys.executable, str(_CHECKER_SCRIPT), "--file", checker_file, "--json"],
            capture_output=True,
            text=True,
        )
    finally:
        if tmp_to_delete is not None:
            os.unlink(tmp_to_delete)

    # exit 0 = approve, exit 1 = needs-fix — both are normal routing outcomes
    # exit 2+ = infrastructure/input error
    if proc.returncode not in (0, 1):
        print(
            f"ERROR: check_issue_contract.py exited with unexpected code "
            f"{proc.returncode}.\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)

    # Parse stdout JSON only — never merge stderr
    try:
        checker_json = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(
            f"ERROR: check_issue_contract.py stdout is not valid JSON: {e}\n"
            f"stdout: {proc.stdout[:500]}",
            file=sys.stderr,
        )
        sys.exit(3)

    return proc.returncode, checker_json


# ---------------------------------------------------------------------------
# Build schema-valid state dict (allowlist enforcement)
# ---------------------------------------------------------------------------


def _derive_fix_category(
    blocking_issues: list[str],
    missing_sections: list[str],
    missing_contract_keys: list[str],
) -> str:
    """Derive a deterministic fix category from checker blockers."""
    if missing_sections:
        return "missing_section"
    if missing_contract_keys:
        return "missing_contract_key"

    for msg in blocking_issues:
        normalized = msg.lower()
        if "contract" in normalized and "fail" in normalized:
            return "unknown_contract_failure"
    return "unknown_contract_failure"



def _load_preflight_artifact(artifact_path: str) -> "dict | None":
    """
    AC8: Load and validate a refinement_preflight_result_v1 artifact.

    Returns the parsed dict on success, or None if:
    - file does not exist / cannot be read
    - JSON parse fails
    - schema_version field is wrong
    """
    try:
        p = Path(artifact_path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != "refinement_preflight_result/v1":
        return None
    return data


def _extract_missing_from_artifact(
    artifact_path: "str | None",
) -> "tuple[list[str], list[str], str | None]":
    """
    AC8: Extract missing_sections and missing_contract_keys from preflight artifact.

    Returns (missing_sections, missing_contract_keys, error_reason_code).
    error_reason_code is None on success; a blocker string on failure.
    """
    if artifact_path is None:
        return [], [], "ARTIFACT_PATH_NOT_PROVIDED"

    artifact = _load_preflight_artifact(artifact_path)
    if artifact is None:
        return [], [], "ARTIFACT_MISSING_OR_INVALID"

    required_sections = artifact.get("required_sections", [])
    required_contract_keys = artifact.get("required_contract_keys", [])
    if not isinstance(required_sections, list):
        return [], [], "ARTIFACT_SCHEMA_INVALID"
    if not isinstance(required_contract_keys, list):
        return [], [], "ARTIFACT_SCHEMA_INVALID"

    return required_sections, required_contract_keys, None


def _build_state_dict(
    *,
    rewrite_attempt_count: int,
    max_rewrite_attempts: int,
    checker_exit_code: int,
    checked_body_sha256: str,
    checker_json: dict,
    previous_state: Optional[LOOP_REWRITE_ROUTER_STATE_V1],
    source_issue_body_sha256: Optional[str],
    source_body_reset: bool,
    preflight_artifact_path: "str | None" = None,
    last_mutation_kind: str = "semantic_rewrite",
) -> "tuple[dict, str | None]":
    """
    Construct a LOOP_REWRITE_ROUTER_STATE_V1 dict from first principles.

    Returns (state_dict, artifact_error). artifact_error is None on success;
    a non-None string when artifact is missing/invalid (B2 fix: callers must
    check and exit early — do not validate/persist on error).

    Only keys in _STATE_ALLOWLIST are set.  checker_json keys are NOT merged
    (AC4c: schema allowlist-outside keys must not enter router state).

    AC8/B3: missing_sections and missing_contract_keys come solely from the
    preflight artifact; the regex fallback path is removed.
    """
    # Extract only the fields we need from checker_json (explicit key access)
    blocking_issues: list[str] = checker_json.get("blocking_issues") or []

    # AC8/B3: artifact is the sole source of truth for missing_sections.
    # Regex fallback removed; callers without artifact get artifact_error and exit 3.
    if preflight_artifact_path is not None:
        missing_sections, missing_contract_keys, artifact_error = (
            _extract_missing_from_artifact(preflight_artifact_path)
        )
    else:
        missing_sections = []
        missing_contract_keys = []
        artifact_error = "ARTIFACT_PATH_NOT_PROVIDED"

    # B1: fix_category computed AFTER both branches (was inside else only — UnboundLocalError on artifact path)
    fix_category = _derive_fix_category(
        blocking_issues,
        missing_sections,
        missing_contract_keys,
    )

    # AC10: budget_debit — format_only_repair does not consume budget
    budget_debit = 0 if last_mutation_kind == "format_only_repair" else 1

    # B5/AC9: fingerprint based on reason_codes + rewrite_constraints (not fix_category + missing sets)
    import hashlib as _hashlib
    _fail_closed = checker_json.get("fail_closed") or {}
    _reason_codes = _fail_closed.get("reason_codes") or []
    _rewrite_constraints_obj = _fail_closed.get("rewrite_constraints") or {}
    _fingerprint_payload = json.dumps(
        {
            "reason_codes": sorted(_reason_codes) if isinstance(_reason_codes, list) else [],
            "rewrite_constraints": _rewrite_constraints_obj,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    rewrite_request_fingerprint = _hashlib.sha256(
        _fingerprint_payload.encode("utf-8")
    ).hexdigest()

    # previous_* fields from loaded state
    previous_checked_body_sha256: Optional[str] = None
    previous_missing_sections: list[str] = []
    previous_missing_contract_keys: list[str] = []
    previous_rewrite_history: list[str] = []
    prev_fingerprints: list[str] = []
    if previous_state is not None:
        if source_body_reset:
            # source body changed by human — reset stale history.
            previous_rewrite_history = []
            prev_fingerprints = []
        else:
            previous_rewrite_history = list(previous_state.rewrite_history)
            # B4: accumulate fingerprint history from previous state
            prev_fp_list = list(previous_state.previous_rewrite_request_fingerprints or [])
            if previous_state.rewrite_request_fingerprint is not None:
                prev_fp_list = prev_fp_list + [previous_state.rewrite_request_fingerprint]
            prev_fingerprints = prev_fp_list
        previous_checked_body_sha256 = previous_state.checked_body_sha256
        previous_missing_sections = list(previous_state.missing_sections)
        previous_missing_contract_keys = list(previous_state.missing_contract_keys)

    rewrite_history = previous_rewrite_history + [fix_category]
    occurrence_count = rewrite_history.count(fix_category)

    state_dict: dict = {
        "schema_version": SCHEMA_VERSION,
        "rewrite_attempt_count": rewrite_attempt_count,
        "max_rewrite_attempts": max_rewrite_attempts,
        "checker_exit_code": checker_exit_code,
        "checked_body_sha256": checked_body_sha256,
        "fix_category": fix_category,
        "rewrite_history": rewrite_history,
        "occurrence_count": occurrence_count,
        "missing_sections": missing_sections,
        "missing_contract_keys": missing_contract_keys,
        "previous_checked_body_sha256": previous_checked_body_sha256,
        "previous_missing_sections": previous_missing_sections,
        "previous_missing_contract_keys": previous_missing_contract_keys,
        "source_issue_body_sha256": source_issue_body_sha256,
        "replay_safe": previous_state is not None,
        "source_body_reset": source_body_reset,
        # B4: AC9/AC10 fields now in JSON schema (loop_rewrite_router_state_v1.json updated)
        "rewrite_request_fingerprint": rewrite_request_fingerprint,
        "previous_rewrite_request_fingerprints": prev_fingerprints,
        "last_mutation_kind": last_mutation_kind,
        "budget_debit": budget_debit,
    }

    # Enforce allowlist (defensive: remove any unexpected keys)
    unexpected = set(state_dict.keys()) - _STATE_ALLOWLIST
    for key in unexpected:
        del state_dict[key]

    # B2: return artifact_error separately so main() can exit 3 BEFORE validate/persist
    return state_dict, artifact_error


# ---------------------------------------------------------------------------
# Router invocation (subprocess, same as orchestrator)
# ---------------------------------------------------------------------------


def _run_router(state_dict: dict) -> dict:
    """
    Invoke decide_rewrite_route.py via subprocess (mirrors orchestrator invocation).

    Returns the RouteResult dict from stdout.
    """
    proc = subprocess.run(
        [sys.executable, str(_ROUTER_SCRIPT)],
        input=json.dumps(state_dict),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 2:
        print(
            f"ERROR: decide_rewrite_route.py rejected input (schema violation).\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(2)
    if proc.returncode != 0:
        print(
            f"ERROR: decide_rewrite_route.py exited with unexpected code "
            f"{proc.returncode}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(
            f"ERROR: decide_rewrite_route.py stdout is not valid JSON: {e}\n"
            f"stdout: {proc.stdout[:500]}",
            file=sys.stderr,
        )
        sys.exit(3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite Loop Router Wrapper — orchestrates checker + router in one call."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--issue", "-i", type=int, help="GitHub Issue 番号"
    )
    source_group.add_argument(
        "--file", "-f", help="Issue body ファイルパス（テスト用）"
    )
    parser.add_argument(
        "--repo", "-r",
        help="GitHub repo (owner/repo)。--issue と共に使用",
    )
    parser.add_argument(
        "--state-path",
        required=True,
        help="router state JSON の永続化パス",
    )
    parser.add_argument(
        "--max-rewrite-attempts",
        type=int,
        required=True,
        help="最大 rewrite 試行回数",
    )
    parser.add_argument(
        "--artifact-path",
        default=None,
        help="AC8: Path to refinement_preflight_result_v1.json artifact. "
             "When provided, missing_sections and missing_contract_keys are read "
             "from the artifact instead of regex extraction from checker prose.",
    )
    parser.add_argument(
        "--mutation-kind",
        default="semantic_rewrite",
        choices=["semantic_rewrite", "format_only_repair", "no_change"],
        help="AC10: Kind of mutation applied (semantic_rewrite/format_only_repair/no_change). "
             "format_only_repair does not consume budget (budget_debit=0).",
    )
    parser.add_argument(
        "--body-readback-confirmed",
        action="store_true",
        help="AC4: 直近の mutation 後に body を再取得(readback)し期待値と一致したことを確認済み。"
             "未指定時は False（fail-closed）。",
    )
    parser.add_argument(
        "--title-readback-confirmed",
        action="store_true",
        help="AC4: 直近の mutation 後に title を再取得(readback)し期待値と一致したことを確認済み。"
             "未指定時は False（fail-closed）。",
    )
    parser.add_argument(
        "--fresh-review-preflight-completed",
        action="store_true",
        help="AC4: readback 後の body/title に対して fresh review/preflight が完了済み。"
             "未指定時は False（fail-closed）。",
    )
    args = parser.parse_args(argv)

    if args.issue and not args.repo:
        print("ERROR: --issue には --repo が必要です", file=sys.stderr)
        sys.exit(2)

    if args.max_rewrite_attempts < 1:
        print(
            f"ERROR: --max-rewrite-attempts must be >= 1, got {args.max_rewrite_attempts}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 1: Fetch / load issue body
    if args.file:
        body = _load_body_from_file(args.file)
    else:
        body = _fetch_issue_body_from_github(args.issue, args.repo)

    # Step 2: Compute body hash
    checked_body_sha256 = _sha256_of_body(body)

    # Step 3: Load persisted router state (replay-safe; fail-closed on corruption)
    try:
        previous_state = load_rewrite_router_state(
            args.state_path,
            current_source_body_sha256=checked_body_sha256,
        )
    except RewriteRouterStateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)

    source_body_reset = (
        previous_state is not None and previous_state.source_body_reset
    )

    # Step 4: Increment attempt counter
    if previous_state is None:
        rewrite_attempt_count = 0
    else:
        # If source body was reset, attempt count was already reset to 0 in the
        # loaded state; we still increment for this invocation.
        rewrite_attempt_count = previous_state.rewrite_attempt_count + 1

    # Step 5: Run checker (stdout JSON only; exit 0/1 are both routed normally)
    # In --file mode, pass original file path so checker's load_fixture_file
    # can read fixture front-matter (LABELS / TITLE) for issue_kind detection.
    checker_exit_code, checker_json = _run_checker(
        body,
        source_file_path=args.file if args.file else None,
    )

    # Step 6: Build schema-valid state dict (allowlist enforcement)
    state_dict, artifact_error = _build_state_dict(
        rewrite_attempt_count=rewrite_attempt_count,
        max_rewrite_attempts=args.max_rewrite_attempts,
        checker_exit_code=checker_exit_code,
        checked_body_sha256=checked_body_sha256,
        checker_json=checker_json,
        previous_state=previous_state,
        source_issue_body_sha256=checked_body_sha256,
        source_body_reset=source_body_reset,
        preflight_artifact_path=args.artifact_path,
        last_mutation_kind=args.mutation_kind,
    )

    # AC8/B2/B3: artifact_error returned by _build_state_dict; includes
    # ARTIFACT_PATH_NOT_PROVIDED when --artifact-path is omitted.
    if artifact_error is not None:
        print(
            f"ERROR: AC8 artifact failure: {artifact_error}",
            file=sys.stderr,
        )
        sys.exit(3)

    # Step 7: Validate state dict against schema
    valid, error_msg = validate_state_dict(state_dict)
    if not valid:
        print(f"ERROR: Built state dict is schema-invalid: {error_msg}", file=sys.stderr)
        sys.exit(2)

    # Step 8: Invoke router
    route_result = _run_router(state_dict)

    # Step 9: Persist state (atomic write; attempt counter survives session restart)
    state_obj = LOOP_REWRITE_ROUTER_STATE_V1(
        rewrite_attempt_count=state_dict["rewrite_attempt_count"],
        max_rewrite_attempts=state_dict["max_rewrite_attempts"],
        checker_exit_code=state_dict["checker_exit_code"],
        checked_body_sha256=state_dict["checked_body_sha256"],
        fix_category=state_dict["fix_category"],
        rewrite_history=state_dict["rewrite_history"],
        occurrence_count=state_dict["occurrence_count"],
        missing_sections=state_dict["missing_sections"],
        missing_contract_keys=state_dict["missing_contract_keys"],
        previous_checked_body_sha256=state_dict["previous_checked_body_sha256"],
        previous_missing_sections=state_dict["previous_missing_sections"],
        previous_missing_contract_keys=state_dict["previous_missing_contract_keys"],
        source_issue_body_sha256=state_dict["source_issue_body_sha256"],
        replay_safe=True,
        source_body_reset=state_dict["source_body_reset"],
        # B4: AC9/AC10 fields now persisted
        rewrite_request_fingerprint=state_dict.get("rewrite_request_fingerprint"),
        previous_rewrite_request_fingerprints=state_dict.get("previous_rewrite_request_fingerprints", []),
        last_mutation_kind=state_dict.get("last_mutation_kind", "semantic_rewrite"),
        budget_debit=state_dict.get("budget_debit", 1),
    )
    save_rewrite_router_state(state_obj, args.state_path)

    # Step 10: AC4 — compute implementation_allowed (fail-closed until
    # body/title readback + fresh review/preflight are confirmed complete).
    route_result["implementation_allowed"] = compute_implementation_allowed(
        route=route_result.get("route"),
        body_readback_confirmed=args.body_readback_confirmed,
        title_readback_confirmed=args.title_readback_confirmed,
        fresh_review_preflight_completed=args.fresh_review_preflight_completed,
    )

    # Step 11: Emit RouteResult JSON to stdout
    print(json.dumps(route_result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
