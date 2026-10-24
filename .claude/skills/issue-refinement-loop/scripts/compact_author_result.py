#!/usr/bin/env python3
"""
compact_author_result.py - Convert raw author result to ISSUE_AUTHOR_RESULT_COMPACT_V1.

Reads ISSUE_AUTHOR_RESULT_V1 JSON from stdin (or --input-file),
writes compact stdout and full artifact JSON.

stdout format (machine-readable compact lines):
  STATUS: ok | failed | no_change
  SUMMARY: <one-line prose>
  BODY_HASH: <sha256 of updated body or empty>
  COMMENT_URL: <url or empty>
  ARTIFACT: compact_author_result_v1=<path>
  NEXT_ACTION: proceed | human_judgment_required

exit codes: 0=ok, 1=no_change, 2=failed / body_hash_missing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _strict_json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_nonfinite_json)

# ---------------------------------------------------------------------------
# Schema constants (SSOT for ISSUE_AUTHOR_RESULT_COMPACT_V1)
# ---------------------------------------------------------------------------

COMPACT_SCHEMA_NAME = "ISSUE_AUTHOR_RESULT_COMPACT_V1"
COMPACT_SCHEMA_VERSION = "1"

REQUIRED_COMPACT_FIELDS = [
    "STATUS",
    "SUMMARY",
    "BODY_HASH",
    "COMMENT_URL",
    "ARTIFACT",
    "NEXT_ACTION",
]

VALID_STATUSES = {"ok", "failed", "no_change"}

# ---------------------------------------------------------------------------
# ISSUE_AUTHOR_RESULT_V1 schema-less consumer contract (AC6 / #1165)
# ---------------------------------------------------------------------------
# compact_author_result.py is a schema-less consumer of ISSUE_AUTHOR_RESULT_V1.
# It does NOT validate against a full JSON schema.
#
# Checked fields and rejection conditions:
#   status:
#     - required
#     - must be one of VALID_STATUSES ("ok", "failed", "no_change")
#     - rejection: missing or invalid value → ValueError → REASON_CODE: schema_mismatch
#   checked_body_sha256 / --updated-body / --updated-body-file:
#     - body_hash source required for status="ok"
#     - rejection: status="ok" and no body_hash source → ValueError → REASON_CODE: schema_mismatch
#
# Fields NOT checked structurally (pass-through to artifact):
#   comment_url, updated_fields, mutation_result, validation_blockers,
#   reflection_notes, parser_gap_repaired, contract_hygiene_repair_applied
#
# This contract is fixture-fixed in test_producer_fail_closed.py.
ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT = {
    "schema_name": "ISSUE_AUTHOR_RESULT_V1",
    "consumer_mode": "schema_less",
    "checked_fields": {
        "status": {
            "required": True,
            "valid_values": list(VALID_STATUSES),
            "rejection_reason_code": "schema_mismatch",
        },
        "body_hash_source": {
            "required_when": "status == ok",
            "sources": ["checked_body_sha256", "--updated-body", "--updated-body-file"],
            "rejection_reason_code": "schema_mismatch",
        },
    },
    "unchecked_fields": [
        "comment_url",
        "updated_fields",
        "mutation_result",
        "validation_blockers",
        "reflection_notes",
        "parser_gap_repaired",
        "contract_hygiene_repair_applied",
    ],
}


def _default_artifact_dir() -> Path:
    return Path(".claude/artifacts/issue-refinement-loop")


def _validate_artifact_path(path: str | Path) -> Path:
    """
    Validate artifact path component: reject .. and absolute paths.

    This validates user-supplied path components, not trusted artifact_dir base paths.
    """
    p = Path(path)
    if p.is_absolute():
        raise ValueError(f"Absolute artifact path rejected: {path}")
    parts = p.parts
    if ".." in parts:
        raise ValueError(f"Path traversal rejected: {path}")
    return p


def _validate_artifact_containment(artifact_path: Path, repo_root: Path) -> None:
    """
    Validate that the resolved artifact path is contained within the expected base directory.

    Uses Path.resolve() to follow symlinks and eliminate '..' before checking containment.
    Raises ValueError if the resolved path escapes the base directory.
    """
    base = (repo_root / ".claude/artifacts/issue-refinement-loop").resolve()
    resolved = artifact_path.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(
            f"Artifact path escapes base directory: resolved={resolved}, base={base}"
        )


def _validate_issue_slot(slot: str) -> None:
    """Validate that the issue slot component does not contain path traversal."""
    if ".." in slot or "/" in slot or "\\" in slot:
        raise ValueError(f"Invalid issue slot: {slot!r}")


def _atomic_write(path: Path, content: bytes) -> None:
    """Write content atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _no_secret_check(text: str) -> list[str]:
    """Detect secret-like strings in output."""
    import re

    patterns = [
        (r"(?i)(Bearer\s+)[A-Za-z0-9\-._~+/]{20,}", "Bearer token"),
        (r"(?i)(Authorization:\s*)[A-Za-z0-9+/=]{20,}", "Authorization header"),
        (r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "API key"),
        (r"(?i)(secret\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "secret value"),
        (r"(?i)(token\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "token value"),
        (r"(?i)(cookie\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "cookie value"),
        (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
        (r"ghs_[A-Za-z0-9._-]{36,}", "GitHub server token"),
    ]
    violations = []
    for pattern, label in patterns:
        if re.search(pattern, text):
            violations.append(label)
    return violations


# ---------------------------------------------------------------------------
# Canonical failure envelope (AC1 / #1165)
# ---------------------------------------------------------------------------


def _write_failure_artifact(
    artifact_dir: Path,
    issue_number: "int | None",
    reason_code: str,
    detail: str,
    repo_root: "Path | None" = None,
    extra: "dict[str, Any] | None" = None,
) -> "tuple[Path, str]":
    """
    Write a PRODUCER_FAILURE_V1 artifact and return (path, sha256).

    AC7: when repo_root is provided, the canonical path
    <repo_root>/.claude/artifacts/issue-refinement-loop/<issue>/ is used.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slot = str(issue_number) if issue_number else "unknown"
    _validate_issue_slot(slot)
    # AC7: use canonical base when repo_root is provided
    if repo_root is not None:
        base = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"
    else:
        base = artifact_dir
    artifact_subdir = base / slot
    artifact_path = artifact_subdir / f"producer_failure_{reason_code}_{ts}.json"
    payload: "dict[str, Any]" = {
        "schema": "PRODUCER_FAILURE_V1",
        "generated_at": ts,
        "reason_code": reason_code,
        "detail": str(detail),
    }
    if extra:
        payload.update(extra)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    sha256 = hashlib.sha256(content).hexdigest()
    _atomic_write(artifact_path, content)
    return artifact_path, sha256


def _emit_failure_envelope(
    reason_code: str,
    next_action: str,
    detail: str,
    artifact_dir: Path,
    issue_number: "int | None",
    repo_root: "Path | None" = None,
    extra: "dict[str, Any] | None" = None,
) -> None:
    """
    Emit canonical failure envelope to stdout and write failure artifact.

    stdout format (always ≤ 2048 UTF-8 bytes):
      STATUS: failed
      NEXT_ACTION: <next_action>
      REASON_CODE: <reason_code>
      ARTIFACT: producer_failure_v1=<path>
      ARTIFACT_SHA256: <sha256>
    """
    artifact_ref = "producer_failure_v1=<write_failed>"
    artifact_sha256 = "write_failed"
    try:
        artifact_path, sha256 = _write_failure_artifact(
            artifact_dir, issue_number, reason_code, detail, repo_root, extra
        )
        artifact_ref = f"producer_failure_v1={artifact_path}"
        artifact_sha256 = sha256
    except Exception:
        pass  # Artifact write failed; emit envelope with write_failed sentinel

    lines = [
        "STATUS: failed",
        f"NEXT_ACTION: {next_action}",
        f"REASON_CODE: {reason_code}",
        f"ARTIFACT: {artifact_ref}",
        f"ARTIFACT_SHA256: {artifact_sha256}",
    ]

    # Enforce 2048-byte cap on the envelope itself; truncate artifact_ref if needed
    envelope_text = "\n".join(lines)
    if len(envelope_text.encode("utf-8")) > 2048:
        short_ref = artifact_ref[:80] + "..." if len(artifact_ref) > 80 else artifact_ref
        lines[3] = f"ARTIFACT: {short_ref}"
        envelope_text = "\n".join(lines)

    for line in lines:
        print(line, flush=True)


def compact_author_result(
    raw_result: dict[str, Any],
    artifact_dir: Path,
    issue_number: int | None = None,
    updated_body: str | None = None,
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], list[str], Path, bytes]:
    """
    Convert raw ISSUE_AUTHOR_RESULT_V1 to ISSUE_AUTHOR_RESULT_COMPACT_V1.

    Returns (compact_data, stdout_lines, artifact_path, artifact_content).
    Does NOT write the artifact — caller must write it AFTER budget check (B3).
    Raises ValueError if:
    - status is missing or unknown/invalid (B1)
    - body_hash is missing for ok status
    - artifact path escapes containment base
    """
    status = raw_result.get("status")
    if status is None:
        raise ValueError(
            "'status' field is required in ISSUE_AUTHOR_RESULT_V1 but was missing"
        )
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Unknown/invalid status: {status!r}. Expected one of {VALID_STATUSES}"
        )

    # Derive body hash: from --updated-body or from raw_result.checked_body_sha256
    body_hash = ""
    if updated_body is not None:
        body_hash = hashlib.sha256(updated_body.encode("utf-8")).hexdigest()
    elif "checked_body_sha256" in raw_result:
        body_hash = raw_result["checked_body_sha256"]

    # body_hash is required for ok status
    if status == "ok" and not body_hash:
        raise ValueError(
            f"body_hash is required for status={status!r} but was not provided. "
            "Pass --updated-body or include checked_body_sha256 in the input."
        )

    # Extract comment URL (if any)
    comment_url = raw_result.get("comment_url", "") or ""

    # Determine NEXT_ACTION
    if status in ("ok", "no_change"):
        next_action = "proceed"
    else:
        next_action = "human_judgment_required"

    # Build one-line SUMMARY (B1: required field)
    if status == "ok":
        summary = "mutation applied"
    elif status == "no_change":
        summary = "no change applied"
    else:
        # failed
        blockers = raw_result.get("validation_blockers", [])
        if blockers:
            first = blockers[0]
            if isinstance(first, dict):
                code = first.get("code", "")
                summary = (
                    f"failed; {len(blockers)} blocker(s); first={code}"
                    if code
                    else f"failed; {len(blockers)} blocker(s)"
                )
            else:
                summary = f"failed; {str(first)[:60]}"
        else:
            summary = "failed"

    # Determine artifact path
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slot = str(issue_number) if issue_number else "unknown"
    _validate_issue_slot(slot)
    artifact_subdir = artifact_dir / slot
    artifact_filename = f"compact_author_result_{ts}.json"
    artifact_path = artifact_subdir / artifact_filename

    # B4: containment check — resolve symlinks and verify artifact stays under base
    if repo_root is not None:
        _validate_artifact_containment(artifact_path, repo_root)

    # Build full artifact JSON
    full_artifact: dict[str, Any] = {
        "schema": COMPACT_SCHEMA_NAME,
        "schema_version": COMPACT_SCHEMA_VERSION,
        "generated_at": ts,
        "status": status,
        "summary": summary,
        "body_hash": body_hash,
        "comment_url": comment_url,
        "next_action": next_action,
        "updated_fields": raw_result.get("updated_fields", []),
        "mutation_result": raw_result.get("mutation_result", {}),
        "unchanged_reason": raw_result.get("unchanged_reason"),
        "validation_blockers": raw_result.get("validation_blockers", []),
        "reflection_notes": raw_result.get("reflection_notes", []),
        "parser_gap_repaired": raw_result.get("parser_gap_repaired", False),
        "contract_hygiene_repair_applied": raw_result.get(
            "contract_hygiene_repair_applied", False
        ),
    }

    # B5: secret check on artifact content before writing
    artifact_content_str = json.dumps(full_artifact, ensure_ascii=False, indent=2, allow_nan=False)
    artifact_violations = _no_secret_check(artifact_content_str)
    if artifact_violations:
        raise ValueError(
            f"secret-like strings detected in artifact content: {artifact_violations}"
        )

    # B3: do NOT write artifact here; caller writes AFTER stdout budget check
    artifact_content = artifact_content_str.encode("utf-8")

    # Build compact dict
    compact_data = {
        "STATUS": status,
        "SUMMARY": summary,
        "BODY_HASH": body_hash,
        "COMMENT_URL": comment_url,
        "ARTIFACT": f"compact_author_result_v1={artifact_path}",
        "NEXT_ACTION": next_action,
    }

    # Build stdout lines
    stdout_lines = [
        f"STATUS: {compact_data['STATUS']}",
        f"SUMMARY: {compact_data['SUMMARY']}",
        f"BODY_HASH: {compact_data['BODY_HASH']}",
    ]
    if compact_data["COMMENT_URL"]:
        stdout_lines.append(f"COMMENT_URL: {compact_data['COMMENT_URL']}")
    stdout_lines.append(f"ARTIFACT: {compact_data['ARTIFACT']}")
    stdout_lines.append(f"NEXT_ACTION: {compact_data['NEXT_ACTION']}")

    return compact_data, stdout_lines, artifact_path, artifact_content


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert ISSUE_AUTHOR_RESULT_V1 to ISSUE_AUTHOR_RESULT_COMPACT_V1"
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        default=None,
        help="Path to ISSUE_AUTHOR_RESULT_V1 JSON (default: stdin)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=_default_artifact_dir(),
        help="Base artifact directory",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="Issue number for artifact sub-directory",
    )
    parser.add_argument(
        "--updated-body",
        type=str,
        default=None,
        help="Updated issue body text (for body_hash computation)",
    )
    parser.add_argument(
        "--updated-body-file",
        type=Path,
        default=None,
        help="Path to updated issue body file (for body_hash computation)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for artifact containment check (B4)",
    )
    args = parser.parse_args()

    # Read input (P1-7: use _strict_json_loads to reject NaN/Infinity)
    try:
        if args.input_file:
            raw_text = args.input_file.read_text(encoding="utf-8")
        else:
            raw_text = sys.stdin.read()
        raw_result = _strict_json_loads(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=f"JSON parse error: {e}",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2
    except Exception as e:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=f"Input read error: {e}",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2

    # Resolve updated_body
    updated_body: str | None = args.updated_body
    if args.updated_body_file:
        try:
            updated_body = args.updated_body_file.read_text(encoding="utf-8")
        except Exception as e:
            _emit_failure_envelope(
                reason_code="schema_mismatch",
                next_action="human_judgment_required",
                detail=f"failed to read --updated-body-file: {e}",
                artifact_dir=args.artifact_dir,
                issue_number=args.issue_number,
                repo_root=args.repo_root,
            )
            return 2

    # Convert (B3: compact_author_result does NOT write artifact)
    try:
        _compact, stdout_lines, artifact_path, artifact_content = compact_author_result(
            raw_result,
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            updated_body=updated_body,
            repo_root=args.repo_root,
        )
    except (ValueError, OSError) as e:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=str(e),
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2

    # Secret check on stdout output
    output_text = "\n".join(stdout_lines)
    violations = _no_secret_check(output_text)
    if violations:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=f"secret-like strings detected in stdout: {violations}",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2

    # B3: enforce 2048 UTF-8 bytes limit BEFORE writing success artifact
    byte_count = len(output_text.encode("utf-8"))
    if byte_count > 2048:
        # AC3: no success artifact written; save details to failure artifact only
        output_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
        _emit_failure_envelope(
            reason_code="output_budget_violation",
            next_action="human_judgment_required",
            detail=f"stdout exceeds 2048 UTF-8 bytes limit: {byte_count} bytes",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
            extra={
                "byte_count": byte_count,
                "output_sha256": output_sha256,
                "bounded_preview": output_text[:256],
            },
        )
        return 2

    # Budget OK: now write success artifact atomically
    _atomic_write(artifact_path, artifact_content)

    # Output
    for line in stdout_lines:
        print(line, flush=True)

    status = raw_result.get("status")
    if status == "no_change":
        return 1
    if status == "failed":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
