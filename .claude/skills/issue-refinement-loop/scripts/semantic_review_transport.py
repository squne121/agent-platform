#!/usr/bin/env python3
"""Production producer for the Step 2.5 semantic design review lane (#2296).

This module owns the ONLY path by which an ``issue-design-reviewer``
invocation's result is bound, validated, and persisted as a
``SEMANTIC_REVIEW_RESULT_V1`` sidecar artifact (P0-1: previously no such
producer existed and an implementation of agent/trigger/schema/router/editor
could ship with the reviewer never actually wired up).

The actual launch of the ``issue-design-reviewer`` SubAgent is performed by
the orchestrator (main/root session) using the Agent tool -- a Python
script cannot itself invoke that tool. This module instead provides the
two halves of the contract around that launch:

1. ``pin_bundle()`` -- called BEFORE the orchestrator launches the SubAgent.
   Pins ``body_sha256`` at the moment deterministic verdict == approve,
   assembles the input bundle (current body + anchor feedback + deterministic
   findings), writes the pinned Issue body verbatim to a sibling ``body.md``
   file in the same invocation directory (P0-1: a fresh subagent context
   does not inherit parent conversation context, so the actual body text
   must be delivered as a file the subagent is instructed to read -- a
   ``body_sha256`` alone is not a delivery mechanism), and writes a
   ``bundle.json`` recording ``pinned_at`` plus ``body_file: "body.md"``.
   There is no cross-invocation result cache/reuse (P1-2, #2296 fix_delta
   iteration 6): every ``pin_bundle()`` call always leads to a fresh review;
   a prior successful result for the same ``(body_sha256, prompt_version,
   requested_model)`` is never substituted for a fresh launch.

2. ``record_result()`` -- called AFTER the orchestrator's Agent tool call
   returns. This function enforces a **completion join barrier**: the
   orchestrator must not call ``record_result()`` until the launched
   SubAgent's output is actually in hand, and the caller-supplied
   ``completed_at`` must be strictly after ``pinned_at``. Whether the
   underlying Agent tool call ran in the foreground or the background is
   NOT something this module asserts or depends on -- Claude Code does not
   document a "foreground launch" contract, so this module never claims to
   structurally prove one (P0-2). The raw model-output file's mtime being
   at or after ``pinned_at`` is checked only as a weak staleness heuristic
   (a stale canned fixture reused verbatim would normally retain an older
   mtime); it is NOT treated as proof of genuine agent execution -- a test
   harness or the orchestrator itself writing the file also satisfies an
   mtime check, so no such claim is made anywhere in this module or its
   docs. Strict JSON parsing rejects duplicate keys. Model output is
   restricted to ``assessment``/``findings`` -- presence of
   ``owner_disposition`` in the RAW model output (not the final bound
   artifact) is rejected (P0-3: model must not self-adjudicate). The fully
   assembled artifact is additionally validated against
   ``schemas/semantic_review_result_v1.schema.json`` via ``jsonschema``,
   plus an explicit correlation invariant (``assessment: clear`` <=>
   ``findings`` empty, ``assessment: findings`` <=> ``findings`` non-empty)
   that is enforced independently of the schema's own ``if``/``then``
   correlation constraint (belt-and-suspenders, #2296 P0-3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "SEMANTIC_REVIEW_RESULT_V1"
BUNDLE_SCHEMA = "SEMANTIC_REVIEW_BUNDLE_V1"
DEFAULT_ARTIFACTS_ROOT = ".claude/artifacts/issue-refinement-loop"
BODY_FILE_NAME = "body.md"

# P0-3: repo-root-relative location of the canonical JSON Schema this module
# validates every persisted artifact against. Resolved relative to this
# file's own path (not CWD) so `record_result()` works regardless of the
# caller's working directory.
_SCHEMA_RELATIVE_PATH = Path("schemas") / "semantic_review_result_v1.schema.json"


def _repo_root() -> Path:
    # .claude/skills/issue-refinement-loop/scripts/semantic_review_transport.py
    #   -> scripts -> issue-refinement-loop -> skills -> .claude -> repo root
    return Path(__file__).resolve().parents[4]


def _load_schema() -> "dict[str, Any]":
    schema_path = _repo_root() / _SCHEMA_RELATIVE_PATH
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> float:
    # Accept the canonical "%Y-%m-%dT%H:%M:%SZ" form used throughout this repo.
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _strict_json_loads(raw: str) -> Any:
    def _reject_dupes(pairs):
        seen: "dict[str, Any]" = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError(f"duplicate key in JSON object: {key!r}")
            seen[key] = value
        return seen

    return json.loads(raw, object_pairs_hook=_reject_dupes)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Issue #3007: strict accepted representations of ``current_body_sha256`` are
# bare lowercase 64-hex or ``sha256:`` + lowercase 64-hex. ``re.fullmatch``
# ensures no leading/trailing whitespace, trailing newline, or case variants
# are tolerated (``$`` would accept a trailing newline, so it is not used).
_CURRENT_BODY_SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


def _normalize_current_body_sha256(value: Any) -> "str | None":
    """Return the bare lowercase 64-hex digest, or ``None`` if malformed."""
    if not isinstance(value, str):
        return None
    match = _CURRENT_BODY_SHA256_RE.fullmatch(value)
    return match.group(1) if match else None


def _invocation_id(body_sha256: str, prompt_version: str, requested_model: str) -> str:
    preimage = f"{body_sha256}:{prompt_version}:{requested_model}".encode("utf-8")
    return _sha256_hex(preimage)[:16]


def _invocation_dir(artifacts_root: Path, issue_number: int, invocation_id: str) -> Path:
    return artifacts_root / str(issue_number) / invocation_id


def pin_bundle(
    *,
    issue_number: int,
    body_text: str,
    prompt_version: str,
    requested_model: str,
    anchor_feedback: "str | None" = None,
    deterministic_findings: "list[dict[str, Any]] | None" = None,
    artifacts_root: "Path | None" = None,
) -> "dict[str, Any]":
    """Pin the input bundle for a fresh semantic review invocation.

    No cross-invocation cache/reuse (P1-2): every call always mkdir's the
    invocation directory, writes a fresh ``body.md`` + ``bundle.json``, and
    expects the caller to launch a fresh ``issue-design-reviewer`` -- a
    previously recorded successful result for the same invocation key is
    never substituted for a relaunch.
    """
    artifacts_root = artifacts_root or Path(DEFAULT_ARTIFACTS_ROOT)
    body_sha256 = _sha256_hex(body_text.encode("utf-8"))
    invocation_id = _invocation_id(body_sha256, prompt_version, requested_model)
    inv_dir = _invocation_dir(artifacts_root, issue_number, invocation_id)

    inv_dir.mkdir(parents=True, exist_ok=True)
    pinned_at = _now_iso()

    # P0-1: persist the actual pinned body text alongside bundle.json so a
    # fresh issue-design-reviewer subagent context (which does not inherit
    # parent conversation context) has a concrete file to read.
    (inv_dir / BODY_FILE_NAME).write_text(body_text, encoding="utf-8")

    bundle = {
        "schema": BUNDLE_SCHEMA,
        "issue_number": issue_number,
        "invocation_id": invocation_id,
        "body_file": BODY_FILE_NAME,
        "body_sha256": body_sha256,
        "prompt_version": prompt_version,
        "requested_model": requested_model,
        "pinned_at": pinned_at,
        "anchor_feedback": anchor_feedback,
        "deterministic_findings": deterministic_findings or [],
    }
    (inv_dir / "bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    return {
        "schema": BUNDLE_SCHEMA,
        "issue_number": issue_number,
        "invocation_id": invocation_id,
        "invocation_dir": str(inv_dir),
        "body_file": BODY_FILE_NAME,
        "body_sha256": body_sha256,
        "prompt_version": prompt_version,
        "requested_model": requested_model,
        "pinned_at": pinned_at,
    }


def _load_bundle(inv_dir: Path) -> "dict[str, Any]":
    bundle_path = inv_dir / "bundle.json"
    if not bundle_path.exists():
        raise FileNotFoundError(f"bundle not found (pin_bundle must run first): {bundle_path}")
    return _strict_json_loads(bundle_path.read_text(encoding="utf-8"))


def record_result(
    *,
    invocation_dir: "str | Path",
    result_file: "str | Path",
    completed_at: str,
    current_body_sha256: str,
) -> "dict[str, Any]":
    """Bind, validate, and persist a raw ``issue-design-reviewer`` result.

    ``current_body_sha256`` is mandatory (P1-2): freshness must always be
    explicitly checked against the invocation's pinned ``body_sha256``, it
    is never optionally skipped. Accepted representations (#3007): bare
    lowercase 64-hex or ``sha256:`` + lowercase 64-hex (``re.fullmatch``);
    both are normalized to bare for comparison. Any other form yields
    ``transport_status: error`` / ``reason_code: invalid_current_body_sha256``
    while the validated artifact (``freshness_valid: false``) is retained.
    """
    inv_dir = Path(invocation_dir)
    bundle = _load_bundle(inv_dir)
    pinned_at_epoch = _parse_iso(bundle["pinned_at"])

    result_path = Path(result_file)
    if not result_path.exists() or result_path.stat().st_size == 0:
        return _error_result(bundle, reason_code="missing_result_file")

    try:
        completed_at_epoch = _parse_iso(completed_at)
    except ValueError:
        return _error_result(bundle, reason_code="malformed_completed_at")

    if completed_at_epoch <= pinned_at_epoch:
        return _error_result(bundle, reason_code="foreground_not_verified")

    # Weak staleness heuristic only (P0-2): this does NOT prove genuine
    # agent execution. It only rejects the narrow case of a result file
    # whose mtime predates the pin, which is enough to catch a naively
    # reused stale fixture but is not a security/authenticity guarantee.
    result_mtime = result_path.stat().st_mtime
    if result_mtime < pinned_at_epoch:
        return _error_result(bundle, reason_code="stale_result_reused")

    try:
        raw_model_output = _strict_json_loads(result_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return _error_result(bundle, reason_code=f"malformed_json:{exc}")

    if not isinstance(raw_model_output, dict):
        return _error_result(bundle, reason_code="model_output_not_object")

    if "owner_disposition" in raw_model_output:
        return _error_result(bundle, reason_code="model_self_disposition_forbidden")

    allowed_top_keys = {"assessment", "findings"}
    extra_keys = set(raw_model_output.keys()) - allowed_top_keys
    if extra_keys:
        return _error_result(bundle, reason_code=f"unexpected_model_keys:{sorted(extra_keys)}")

    assessment = raw_model_output.get("assessment")
    if assessment not in ("clear", "findings"):
        return _error_result(bundle, reason_code="invalid_assessment")

    findings = raw_model_output.get("findings") or []
    if not isinstance(findings, list):
        return _error_result(bundle, reason_code="invalid_findings_shape")
    for finding in findings:
        if not isinstance(finding, dict):
            return _error_result(bundle, reason_code="invalid_finding_entry")
        if "owner_disposition" in finding:
            return _error_result(bundle, reason_code="model_self_disposition_forbidden")
        if finding.get("severity") not in ("blocker", "high", "medium", "low"):
            return _error_result(bundle, reason_code="invalid_finding_severity")
        if not finding.get("summary"):
            return _error_result(bundle, reason_code="invalid_finding_summary")

    # P0-3: explicit correlation invariant, independent of the schema's own
    # if/then constraint below (belt-and-suspenders).
    if assessment == "clear" and findings:
        return _error_result(bundle, reason_code="assessment_findings_mismatch:clear_with_findings")
    if assessment == "findings" and not findings:
        return _error_result(bundle, reason_code="assessment_findings_mismatch:findings_empty")

    # Issue #3007: the caller digest is validated only AFTER the raw reviewer
    # result passed every check above, so an invalid raw result keeps its
    # existing rejection reason. A malformed caller digest is a distinct
    # input error (not body drift), but the validated assessment/findings are
    # still persisted so a blocker/high finding is never silently dropped.
    normalized_current = _normalize_current_body_sha256(current_body_sha256)
    digest_format_valid = normalized_current is not None
    freshness_valid = digest_format_valid and normalized_current == bundle["body_sha256"]

    artifact = {
        "schema": SCHEMA,
        "issue_number": bundle["issue_number"],
        "body_sha256": bundle["body_sha256"],
        "prompt_version": bundle["prompt_version"],
        "requested_model": bundle["requested_model"],
        "assessment": assessment,
        "findings": findings,
        "artifact_valid": True,
        "input_binding_valid": True,
        "freshness_valid": freshness_valid,
    }

    try:
        import jsonschema

        jsonschema.validate(instance=artifact, schema=_load_schema())
    except Exception as exc:  # jsonschema.ValidationError or loader failure
        return _error_result(bundle, reason_code=f"schema_validation_failed:{exc}")

    (inv_dir / "semantic_review_result.json").write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    if not digest_format_valid:
        return {
            "schema": "SEMANTIC_REVIEW_TRANSPORT_RESULT_V1",
            "transport_status": "error",
            "reason_code": "invalid_current_body_sha256",
            "artifact": artifact,
            "issue_number": bundle.get("issue_number"),
            "invocation_id": bundle.get("invocation_id"),
        }
    transport_status = "ok" if freshness_valid else "stale"
    return {
        "schema": "SEMANTIC_REVIEW_TRANSPORT_RESULT_V1",
        "transport_status": transport_status,
        "reason_code": None,
        "artifact": artifact,
    }


def _error_result(bundle: "dict[str, Any]", *, reason_code: str) -> "dict[str, Any]":
    return {
        "schema": "SEMANTIC_REVIEW_TRANSPORT_RESULT_V1",
        "transport_status": "error",
        "reason_code": reason_code,
        "artifact": None,
        "issue_number": bundle.get("issue_number"),
        "invocation_id": bundle.get("invocation_id"),
    }


def _cmd_pin_bundle(args: argparse.Namespace) -> int:
    body_text = Path(args.body_file).read_text(encoding="utf-8")
    anchor_feedback = None
    if args.anchor_feedback_file:
        anchor_feedback = Path(args.anchor_feedback_file).read_text(encoding="utf-8")
    deterministic_findings = None
    if args.deterministic_findings_file:
        deterministic_findings = _strict_json_loads(
            Path(args.deterministic_findings_file).read_text(encoding="utf-8")
        )
    result = pin_bundle(
        issue_number=args.issue_number,
        body_text=body_text,
        prompt_version=args.prompt_version,
        requested_model=args.requested_model,
        anchor_feedback=anchor_feedback,
        deterministic_findings=deterministic_findings,
        artifacts_root=Path(args.artifacts_root) if args.artifacts_root else None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_record_result(args: argparse.Namespace) -> int:
    result = record_result(
        invocation_dir=args.invocation_dir,
        result_file=args.result_file,
        completed_at=args.completed_at,
        current_body_sha256=args.current_body_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["transport_status"] in ("ok", "stale") else 1


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    pin = sub.add_parser("pin-bundle")
    pin.add_argument("--issue-number", type=int, required=True)
    pin.add_argument("--body-file", required=True)
    pin.add_argument("--prompt-version", required=True)
    pin.add_argument("--requested-model", required=True)
    pin.add_argument("--anchor-feedback-file")
    pin.add_argument("--deterministic-findings-file")
    pin.add_argument("--artifacts-root")
    pin.set_defaults(func=_cmd_pin_bundle)

    record = sub.add_parser("record-result")
    record.add_argument("--invocation-dir", required=True)
    record.add_argument("--result-file", required=True)
    record.add_argument("--completed-at", required=True)
    record.add_argument("--current-body-sha256", required=True)
    record.set_defaults(func=_cmd_record_result)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
