#!/usr/bin/env python3
"""Normalize blocked contract evidence into CONTRACT_BLOCKER_TRIAGE_V1."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


_REGISTRY_DIR = Path(__file__).resolve().parents[2] / "issue-contract-review" / "scripts"
if str(_REGISTRY_DIR) not in sys.path:
    sys.path.insert(0, str(_REGISTRY_DIR))
import pnpm_gate_registry as registry  # noqa: E402


SCHEMA_NAME = "CONTRACT_BLOCKER_TRIAGE_V1"
SCHEMA_VERSION = 1
BASELINE_SCHEMA = "baseline_vc_preflight/v1"
CI_TRUE_DELTA = {"CI": "true"}
# The registry is the sole canonicalization authority.  This compatibility
# view is intentionally derived from it for diagnostics only.
_CANONICAL_PNPM_GATES: list[list[str]] = [
    list(gate.request_argv) for gate in registry.iter_gate_descriptors()
]
VALID_INPUT_SCHEMAS = {
    "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
    "CONTRACT_REVIEW_ONCE_RESULT_V1",
    "CONTRACT_REVIEW_RESULT_V1",
    BASELINE_SCHEMA,
}
VALID_STATUS = {
    "ok",
    "incomplete_evidence",
    "unsupported_input",
    "invalid_input",
}


def _result(
    *,
    status: str,
    input_schema: str | None,
    unsupported_reason: str | None,
    vc_preflight_executed: bool,
    evidence_complete: bool,
    accepted_item_count: int,
    rejected_item_count: int,
    aggregate_reason: str = "unclassified",
    summary: str = "",
    per_ac: list[dict[str, Any]] | None = None,
    suggested_actions: list[dict[str, Any]] | None = None,
    issue_refinement_recommended: bool = False,
    environment_retry_recommended: bool = False,
    body_author_fixable: bool = False,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    suggested_actions = suggested_actions or []
    per_ac = per_ac or []
    errors = errors or []
    suggested_next_action = "human_review"
    if status == "ok":
        if aggregate_reason == "vc_design_requires_refinement":
            suggested_next_action = "refine_issue_contract"
        elif aggregate_reason == "environment_artifact":
            suggested_next_action = (
                "retry_with_runner_env_delta"
                if environment_retry_recommended
                else "inspect_package_manager_state"
            )
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "aggregate_reason": aggregate_reason,
        "step1_allowed": False,
        "termination_reason": "intake_gate_failed",
        "intake_gate_subreason": "missing_contract_go",
        "issue_refinement_recommended": issue_refinement_recommended,
        "environment_retry_recommended": environment_retry_recommended,
        "body_author_fixable": body_author_fixable,
        "suggested_next_action": suggested_next_action,
        "summary": summary,
        "suggested_actions": suggested_actions,
        "per_ac": per_ac,
        "source_integrity": {
            "input_schema": input_schema,
            "vc_preflight_executed": vc_preflight_executed,
            "evidence_complete": evidence_complete,
            "accepted_item_count": accepted_item_count,
            "rejected_item_count": rejected_item_count,
            "unsupported_reason": unsupported_reason,
        },
        "mutation_free": True,
        "errors": errors,
    }


def _sha256_command(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


def _is_valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 71 and value.startswith("sha256:")


def _load_payload(input_file: str | None) -> Any:
    if input_file:
        with open(input_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


def _extract_vc_from_contract_review_result(payload: dict[str, Any]) -> tuple[list[Any] | None, str | None]:
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return None, "missing_checks"
    vc = checks.get("vc_preflight")
    if isinstance(vc, str):
        return None, "scalar_vc_preflight_only"
    if not isinstance(vc, dict):
        return None, "missing_classifications"
    classifications = vc.get("classifications")
    if not isinstance(classifications, list):
        return None, "missing_classifications"
    return classifications, None


def extract_supported_items(payload: Any) -> tuple[str | None, list[Any] | None, bool, str | None]:
    if not isinstance(payload, dict):
        return None, None, False, "top_level_not_object"

    schema = payload.get("schema")
    if schema == "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1":
        review_once = payload.get("contract_review_once_result")
        if payload.get("source") == "latest_blocked" and payload.get("contract_snapshot_url"):
            return schema, None, False, "latest_blocked_snapshot_only"
        if not isinstance(review_once, dict):
            return schema, None, False, "missing_contract_review_once_result"
        classifications = review_once.get("vc_preflight_classifications")
        if not isinstance(classifications, list):
            return schema, None, False, "missing_classifications"
        return schema, classifications, True, None

    if schema == "CONTRACT_REVIEW_ONCE_RESULT_V1":
        classifications = payload.get("vc_preflight_classifications")
        if not isinstance(classifications, list):
            return schema, None, False, "missing_classifications"
        return schema, classifications, True, None

    if schema == "CONTRACT_REVIEW_RESULT_V1":
        classifications, err = _extract_vc_from_contract_review_result(payload)
        if err is not None:
            return schema, None, False, err
        return schema, classifications, True, None

    if schema == BASELINE_SCHEMA:
        results = payload.get("results")
        if not isinstance(results, list):
            return schema, None, False, "missing_classifications"
        return schema, results, True, None

    if schema in VALID_INPUT_SCHEMAS:
        return schema, None, False, "missing_classifications"
    return schema, None, False, "unknown_input_schema"


def _normalize_runner_env_delta(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _normalize_action(item: dict[str, Any]) -> dict[str, Any]:
    if item["category"] == "package_manager_no_tty_prompt":
        if item["runner_env_delta_seen"] == CI_TRUE_DELTA:
            return {
                "kind": "inspect_package_manager_state",
                "argv": None,
                "env_delta": {},
                "preconditions": ["runner_env_delta == {'CI': 'true'}"],
                "reason": "CI=true was already injected; inspect package manager or node_modules state",
            }
        # Use the actual failing gate argv if available; fall back to pnpm build
        # only when raw_command was not present in the input (legacy fallback).
        gate_argv: list[str] | None = item.get("gate_argv") or ["pnpm", "build"]
        gate_str = " ".join(gate_argv) if gate_argv else "pnpm build"
        return {
            "kind": "retry_with_runner_env_delta",
            "argv": gate_argv,
            "env_delta": CI_TRUE_DELTA,
            "preconditions": ["runner_env_delta was absent"],
            "reason": f"Retry the canonical {gate_str} gate with the fixed runner environment delta",
        }
    if item["category"] == "vc_no_tests_collected":
        return {
            "kind": "refine_issue_contract",
            "argv": None,
            "env_delta": {},
            "preconditions": ["vc_preflight category == vc_no_tests_collected"],
            "reason": "pytest exit 5 requires VC design refinement before Step 1",
        }
    return {
        "kind": "human_review",
        "argv": None,
        "env_delta": {},
        "preconditions": [],
        "reason": f"Unsupported blocked category requires human review: {item['category']}",
    }


def normalize_item(item: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        return None, "non_object_item"
    ac = item.get("ac")
    category = item.get("category")
    decision = item.get("decision")
    if not isinstance(ac, str) or not isinstance(category, str) or not isinstance(decision, str):
        return None, "missing_required_keys"
    if decision != "blocked":
        return None, "not_blocked"

    runner_env_delta = _normalize_runner_env_delta(item.get("runner_env_delta"))
    command_hash = item.get("command_hash")
    raw_command = item.get("raw_command")
    if _is_valid_sha256(command_hash):
        normalized_command_hash = command_hash
    elif isinstance(raw_command, str) and raw_command:
        normalized_command_hash = _sha256_command(raw_command)
    else:
        return None, "missing_command_hash"

    if category == "vc_no_tests_collected":
        exit_code = item.get("exit_code")
        subreason = item.get("subreason")
        if isinstance(subreason, str) and subreason:
            normalized_subreason = subreason
        elif exit_code == 5 and isinstance(raw_command, str) and " -k " in raw_command:
            normalized_subreason = "pytest_k_filter_matches_no_tests"
        else:
            return None, "missing_vc_no_tests_collected_subreason"
        triage_reason = "vc_design_requires_refinement"
        body_author_fixable = True
        environment_retry_recommended = False
        evidence_pattern = "pytest_exit_5"
    elif category == "package_manager_no_tty_prompt":
        normalized_subreason = (
            "ci_runner_delta_already_applied"
            if runner_env_delta == CI_TRUE_DELTA
            else "ci_runner_delta_missing"
        )
        triage_reason = "environment_artifact"
        body_author_fixable = False
        environment_retry_recommended = runner_env_delta != CI_TRUE_DELTA
        evidence_pattern = "pnpm_no_tty"
        # A current producer marks its result as evidence-required.  That
        # shape has no fallback: raw command text is not authorization.
        # Marker-less historical fixtures retain their previous routing until
        # they are regenerated, but still derive their canonical argv from the
        # registry rather than a second allowlist.
        if isinstance(raw_command, str) and raw_command.strip():
            import shlex as _shlex
            try:
                _argv = _shlex.split(raw_command.strip())
            except ValueError:
                _argv = []
            if item.get("pnpm_gate_evidence_required") is True:
                _gate, _evidence_error = registry.validate_evidence(
                    item.get("pnpm_gate_evidence"), _argv, str(Path.cwd())
                )
                if _gate is None:
                    return None, _evidence_error or "pnpm_gate_evidence_invalid"
            else:
                _gate = registry.gate_for_request(_argv)
                if _gate is None:
                    return None, "non_canonical_pnpm_gate"
            _gate_argv: list[str] = list(_gate.request_argv)
        else:
            if item.get("pnpm_gate_evidence_required") is True:
                return None, "pnpm_gate_evidence_missing_or_invalid"
            # Historical evidence did not carry argv. Preserve this routing
            # only for marker-less payloads; current producers never use it.
            _gate_argv = ["pnpm", "build"]
    else:
        normalized_subreason = "unsupported_blocker_category"
        triage_reason = "unclassified"
        body_author_fixable = False
        environment_retry_recommended = False
        evidence_pattern = "unknown"

    normalized: dict[str, Any] = {
        "ac": ac,
        "command_hash": normalized_command_hash,
        "category": category,
        "decision": "blocked",
        "triage_reason": triage_reason,
        "subreason": normalized_subreason,
        "evidence_pattern": evidence_pattern,
        "runner_env_delta_seen": runner_env_delta,
        "body_author_fixable": body_author_fixable,
        "environment_retry_recommended": environment_retry_recommended,
    }
    # Attach gate_argv for package_manager_no_tty_prompt so _normalize_action can
    # return the actual failing gate's argv instead of a hardcoded default.
    if category == "package_manager_no_tty_prompt":
        normalized["gate_argv"] = _gate_argv  # type: ignore[possibly-undefined]
    return normalized, None


def summarize(per_ac: list[dict[str, Any]], aggregate_reason: str) -> str:
    if not per_ac:
        return "Blocked evidence is incomplete and cannot be triaged deterministically."
    parts = []
    for entry in per_ac[:3]:
        if entry["category"] == "vc_no_tests_collected":
            parts.append(f"{entry['ac']} pytest exit 5 requires VC refinement")
        elif entry["category"] == "package_manager_no_tty_prompt":
            parts.append(f"{entry['ac']} pnpm no-TTY is an environment artifact")
        else:
            parts.append(f"{entry['ac']} requires human review")
    if aggregate_reason == "mixed":
        parts.append("mixed routing keeps Step 1 blocked")
    return "; ".join(parts) + "."


def build_triage_result(payload: Any) -> dict[str, Any]:
    input_schema, items, vc_preflight_executed, extraction_error = extract_supported_items(payload)
    if extraction_error in {"top_level_not_object"}:
        return _result(
            status="invalid_input",
            input_schema=input_schema,
            unsupported_reason=extraction_error,
            vc_preflight_executed=False,
            evidence_complete=False,
            accepted_item_count=0,
            rejected_item_count=0,
            summary="Input JSON must be an object.",
            errors=["input JSON must be an object"],
        )
    if extraction_error in {
        "unknown_input_schema",
        "scalar_vc_preflight_only",
        "latest_blocked_snapshot_only",
    }:
        return _result(
            status="unsupported_input",
            input_schema=input_schema,
            unsupported_reason=extraction_error,
            vc_preflight_executed=False,
            evidence_complete=False,
            accepted_item_count=0,
            rejected_item_count=0,
            summary="Unsupported input for deterministic contract blocker triage.",
            errors=[extraction_error],
        )
    if items is None:
        return _result(
            status="incomplete_evidence",
            input_schema=input_schema,
            unsupported_reason=extraction_error,
            vc_preflight_executed=vc_preflight_executed,
            evidence_complete=False,
            accepted_item_count=0,
            rejected_item_count=0,
            summary="Blocked evidence is incomplete because required classifications are missing.",
            errors=[extraction_error or "missing_classifications"],
        )

    accepted: list[dict[str, Any]] = []
    rejected_reasons: list[str] = []
    for item in items:
        normalized, reason = normalize_item(item)
        if normalized is None:
            rejected_reasons.append(reason or "rejected_item")
            continue
        accepted.append(normalized)

    if not accepted:
        return _result(
            status="incomplete_evidence",
            input_schema=input_schema,
            unsupported_reason="no_blocked_items",
            vc_preflight_executed=vc_preflight_executed,
            evidence_complete=False,
            accepted_item_count=0,
            rejected_item_count=len(rejected_reasons),
            summary="Blocked evidence does not contain any valid blocked classifications.",
            errors=rejected_reasons,
        )

    triage_reasons = {item["triage_reason"] for item in accepted}
    if triage_reasons == {"vc_design_requires_refinement"}:
        aggregate_reason = "vc_design_requires_refinement"
    elif triage_reasons == {"environment_artifact"}:
        aggregate_reason = "environment_artifact"
    elif len(triage_reasons) == 1 and "unclassified" in triage_reasons:
        aggregate_reason = "unclassified"
    else:
        aggregate_reason = "mixed"

    actions: list[dict[str, Any]] = []
    seen = set()
    for item in accepted:
        action = _normalize_action(item)
        key = (
            action["kind"],
            tuple(action["argv"] or []),
            tuple(sorted(action["env_delta"].items())),
            tuple(action["preconditions"]),
            action["reason"],
        )
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    if aggregate_reason in {"mixed", "unclassified"}:
        actions.append(
            {
                "kind": "human_review",
                "argv": None,
                "env_delta": {},
                "preconditions": [],
                "reason": "Mixed or unclassified blocked evidence must be reviewed before Step 1",
            }
        )

    return _result(
        status="ok",
        input_schema=input_schema,
        unsupported_reason=None,
        vc_preflight_executed=vc_preflight_executed,
        evidence_complete=True,
        accepted_item_count=len(accepted),
        rejected_item_count=len(rejected_reasons),
        aggregate_reason=aggregate_reason,
        summary=summarize(accepted, aggregate_reason),
        per_ac=accepted,
        suggested_actions=actions,
        issue_refinement_recommended=any(
            item["triage_reason"] == "vc_design_requires_refinement" for item in accepted
        ),
        environment_retry_recommended=any(
            item["environment_retry_recommended"] for item in accepted
        ),
        body_author_fixable=all(item["body_author_fixable"] for item in accepted),
        errors=rejected_reasons,
    )


triage_contract_blockers = build_triage_result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize blocked contract evidence without mutating GitHub or rerunning preflight."
    )
    parser.add_argument("--input-file", help="Read input JSON from file. Omit to read from stdin.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = _load_payload(args.input_file)
    except json.JSONDecodeError as exc:
        result = _result(
            status="invalid_input",
            input_schema=None,
            unsupported_reason="json_decode_error",
            vc_preflight_executed=False,
            evidence_complete=False,
            accepted_item_count=0,
            rejected_item_count=0,
            summary="Input JSON could not be decoded.",
            errors=[str(exc)],
        )
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1
    except OSError as exc:
        result = _result(
            status="invalid_input",
            input_schema=None,
            unsupported_reason="input_io_error",
            vc_preflight_executed=False,
            evidence_complete=False,
            accepted_item_count=0,
            rejected_item_count=0,
            summary="Input file could not be read.",
            errors=[str(exc)],
        )
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1

    result = build_triage_result(payload)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
