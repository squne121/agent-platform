#!/usr/bin/env python3
"""
validate_review_compact_output.py - REVIEW_COMPACT_VALIDATION_RESULT_V1

Deterministically validates that the parent-produced compact wire
(`ISSUE_REVIEW_RESULT_COMPACT_V2`) exactly matches the canonical envelope
grammar, so that the `issue-refinement-loop` orchestrator can fail-closed to
`human_judgment_required` instead of accepting fabricated / malformed prose.

Issue #2054 AC5/AC8: this module is a thin delegate over
`reviewer_transport.py` (the V2 contract SSOT grammar/artifact-binding
owner) -- it does not reimplement wire parsing itself. The V1 grammar
(`ISSUE_REVIEW_RESULT_COMPACT_V1`, 9 lines, `EVIDENCE` field,
`compact_review_result.py`) is retired: there is no partial deployment or
downgrade fallback. Transport/producer failure is never mixed into this
success-only envelope; it is reported separately as a parent-owned
`REVIEWER_ATTEMPT_RESULT_V1` (see `reviewer_transport.py`).

Envelope (SSOT: `reviewer_transport.py`, exact 11-line grammar):

    SCHEMA: ISSUE_REVIEW_RESULT_COMPACT_V2
    STATUS: ok
    VERDICT: approve | needs-fix
    SUMMARY: <canonical one-line value>
    BLOCKERS: <non-negative integer>
    NEXT_ACTION: proceed | request_changes
    MUST_READ: <empty or canonical paths>
    REVIEWED_BODY_SHA256: sha256:<64 lowercase hex>
    ATTEMPT_ID: <parent-generated opaque ID>
    ARTIFACT: compact_review_result_v2=<canonical relative path>
    ARTIFACT_SHA256: sha256:<64 lowercase hex>

Any input that does not match this grammar exactly (missing / duplicate /
unknown / out-of-order fields, leading/trailing prose, Markdown code fences,
blank lines, ANSI escapes, NUL / other control characters, input exceeding
2048 UTF-8 bytes, whitespace around keys/values, cross-field invariant
violations) is rejected as `validation_status: invalid` /
`next_action: human_judgment_required` (fail-closed).

Usage:
    <parent-produced V2 wire> | uv run python3 validate_review_compact_output.py --issue-number <N>
    uv run python3 validate_review_compact_output.py --input-file <path> --issue-number <N> \
        [--invocation-id <id>] [--attempt <n>]

stdout: exactly one JSON object, schema `REVIEW_COMPACT_VALIDATION_RESULT_V1`.
Human-oriented diagnostics (if any) go to stderr only; stdout is
machine-only and MUST be parsed as JSON by callers.

Exit codes:
    0 - valid                                  (validation_status: valid)
    1 - contract-invalid                       (validation_status: invalid)
    2 - validator runtime/input/environment error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reviewer_transport as _transport  # noqa: E402

SCHEMA = "REVIEW_COMPACT_VALIDATION_RESULT_V1"
SCHEMA_VERSION = "1"

MAX_INPUT_BYTES = 2048


def validate_review_compact_output(
    raw_text: str,
    *,
    issue_number: int | None = None,
    invocation_id: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Validate `raw_text` against the canonical V2 envelope grammar.

    Delegates to `reviewer_transport.validate_compact_v2()` (Issue #2054
    AC8 SSOT). Returns a dict with keys: validation_status, envelope_kind,
    normalized_payload, violations, next_action, artifact_path_policy --
    the same shape this module has always returned, adapted from V2's
    result shape so existing callers (`decide_next_loop_action.py`,
    `run_root_review_pipeline.classify_child_stdout()`, `SKILL.md` Step 2)
    do not need to branch on schema version.
    """
    result = _transport.validate_compact_v2(
        raw_text, issue_number=issue_number, invocation_id=invocation_id, attempt=attempt
    )

    if result["validation_status"] != "valid":
        violations = list(result["violations"])
        if raw_text == "":
            violations = [{"code": "empty_input", "classification": "reviewer_transport_failure"}]
        return {
            "validation_status": "invalid",
            "envelope_kind": "unknown",
            "normalized_payload": None,
            "violations": violations,
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }

    payload = result["normalized_payload"]
    envelope_kind = "approve" if payload["VERDICT"] == "approve" else "needs_fix"
    return {
        "validation_status": "valid",
        "envelope_kind": envelope_kind,
        "normalized_payload": payload,
        "violations": [],
        "next_action": payload["NEXT_ACTION"],
        "artifact_path_policy": {"status": "valid", "path": payload["ARTIFACT"]},
    }


def build_result(
    raw_bytes: bytes,
    *,
    issue_number: int | None = None,
    invocation_id: str | None = None,
    attempt: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Build the full REVIEW_COMPACT_VALIDATION_RESULT_V1 payload + exit code."""
    input_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    input_byte_count = len(raw_bytes)

    try:
        raw_text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        payload = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "validation_status": "invalid",
            "envelope_kind": "runtime_error",
            "input_sha256": f"sha256:{input_sha256}",
            "input_byte_count": input_byte_count,
            "normalized_payload": None,
            "violations": [{"code": "utf8_decode_error", "detail": str(exc)}],
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }
        return payload, 2

    inner = validate_review_compact_output(
        raw_text, issue_number=issue_number, invocation_id=invocation_id, attempt=attempt
    )
    payload = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "input_sha256": f"sha256:{input_sha256}",
        "input_byte_count": input_byte_count,
        **inner,
    }

    exit_code = 0 if payload["validation_status"] == "valid" else 1
    return payload, exit_code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        int_value = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}") from exc
    if int_value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return int_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the parent-produced ISSUE_REVIEW_RESULT_COMPACT_V2 wire."
    )
    parser.add_argument(
        "--input-file",
        default=None,
        help="Path to the raw compact V2 wire bytes (default: read from stdin).",
    )
    parser.add_argument(
        "--issue-number",
        type=_positive_int,
        required=True,
        help="Active issue number (positive integer). Binds the ARTIFACT issue segment.",
    )
    parser.add_argument("--invocation-id", default=None, help="Bind ARTIFACT invocation-id segment.")
    parser.add_argument("--attempt", type=_positive_int, default=None, help="Bind ARTIFACT attempt segment.")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        if args.input_file:
            with open(args.input_file, "rb") as f:
                raw_bytes = f.read()
        else:
            raw_bytes = sys.stdin.buffer.read()
    except OSError as exc:
        payload = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "validation_status": "invalid",
            "envelope_kind": "runtime_error",
            "input_sha256": None,
            "input_byte_count": None,
            "normalized_payload": None,
            "violations": [{"code": "input_read_error", "detail": str(exc)}],
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        sys.stdout.write("\n")
        return 2

    payload, exit_code = build_result(
        raw_bytes, issue_number=args.issue_number, invocation_id=args.invocation_id, attempt=args.attempt
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
