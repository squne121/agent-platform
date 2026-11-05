#!/usr/bin/env python3
"""Read-only runtime verifier for Issue #2699 AC12."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = "squne121/agent-platform"
ISSUE_NUMBER = 2119
PR_NUMBER = 2137


def _run(argv: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_evidence_module() -> Any | None:
    """Load implementation_landed_evidence.py's production functions.

    #2699 P1-5 (Finding J / AC12): this verifier's live fetch (issue/PR
    metadata, merge-commit main-ancestry compare) stays verifier-owned, but
    the parse/normalize/disposition judgement itself must go through the
    production `derive_landing_disposition()` / `coverage_from_pr_body()`
    rather than a verifier-local inline ternary duplicating that logic.
    """
    import importlib.util

    path = Path(__file__).resolve().with_name("implementation_landed_evidence.py")
    spec = importlib.util.spec_from_file_location("implementation_landed_evidence_for_runtime_verifier", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def verify(*, artifact_path: Path, run_command=_run) -> tuple[dict[str, Any], int]:
    records: list[dict[str, Any]] = []

    def collect(name: str, argv: list[str]) -> tuple[int, str, str]:
        rc, stdout, stderr = run_command(argv)
        records.append(
            {
                "name": name,
                "command": argv,
                "exit_code": rc,
                "stdout_sha256": _digest(stdout),
                "stderr_sha256": _digest(stderr),
            }
        )
        return rc, stdout, stderr

    issue_argv = ["gh", "issue", "view", str(ISSUE_NUMBER), "--repo", REPO, "--json", "number,url,body"]
    pr_argv = [
        "gh",
        "pr",
        "view",
        str(PR_NUMBER),
        "--repo",
        REPO,
        "--json",
        "number,url,state,mergeCommit,closingIssuesReferences,body",
    ]
    issue_rc, issue_out, issue_err = collect("issue_metadata", issue_argv)
    pr_rc, pr_out, pr_err = collect("pr_metadata", pr_argv)
    payload: dict[str, Any] = {
        "schema": "IMPLEMENTATION_LANDED_RUNTIME_VERIFICATION_V1",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_urls": [
            f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
            f"https://github.com/{REPO}/pull/{PR_NUMBER}",
        ],
        "commands": records,
        "fallback_used": False,
    }
    if issue_rc != 0 or pr_rc != 0:
        payload.update(
            {
                "status": "SKIP",
                "reason": "github_read_transport_unavailable",
                "errors": [issue_err.strip(), pr_err.strip()],
            }
        )
        _write_artifact(artifact_path, payload)
        return payload, 77
    try:
        issue = json.loads(issue_out)
        pr = json.loads(pr_out)
    except json.JSONDecodeError as exc:
        payload.update({"status": "SKIP", "reason": "github_read_transport_invalid_json", "errors": [str(exc)]})
        _write_artifact(artifact_path, payload)
        return payload, 77
    merge_oid = (pr.get("mergeCommit") or {}).get("oid") if isinstance(pr.get("mergeCommit"), dict) else None
    if not isinstance(merge_oid, str) or len(merge_oid) != 40:
        payload.update({"status": "FAIL", "reason": "merged_pr_missing_merge_oid", "errors": []})
        _write_artifact(artifact_path, payload)
        return payload, 1
    compare_argv = ["gh", "api", f"repos/{REPO}/compare/{merge_oid}...main", "--jq", ".status"]
    compare_rc, compare_out, compare_err = collect("merge_ancestry", compare_argv)
    if compare_rc != 0:
        payload.update(
            {"status": "SKIP", "reason": "github_read_transport_unavailable", "errors": [compare_err.strip()]}
        )
        _write_artifact(artifact_path, payload)
        return payload, 77
    closing = any(
        isinstance(ref, dict) and ref.get("number") == ISSUE_NUMBER and ref.get("url") == issue.get("url")
        for ref in (pr.get("closingIssuesReferences") or [])
    )
    ancestry = compare_out.strip() in {"ahead", "identical"}

    evidence_module = _load_evidence_module()
    if evidence_module is None:
        payload.update({"status": "FAIL", "reason": "evidence_module_unavailable", "errors": []})
        _write_artifact(artifact_path, payload)
        return payload, 1

    issue_body = str(issue.get("body") or "")
    pr_body = str(pr.get("body") or "")
    coverage = evidence_module.coverage_from_pr_body(
        pr_body=pr_body, issue_number=ISSUE_NUMBER, live_issue_body=issue_body
    )
    # This live historical PR predates the producer. Treating a missing
    # marker as exact coverage would be the forbidden merge-time
    # reconstruction path -- `coverage["status"]` is the production parser's
    # own judgement of marker absence, not a verifier-local body substring
    # search.
    legacy_marker_missing = coverage.get("status") == "missing_marker"
    candidate = {
        "target": {"repo": REPO, "issue_number": ISSUE_NUMBER},
        "pr": {"number": pr.get("number"), "url": pr.get("url"), "head_sha": None},
        "provenance": {"kind": "closing_relation" if closing else "verified_cross_reference", "verified": True},
        "lifecycle": "merged",
        "head_fresh": True,
        "current_scope_ownership": False,
        "merge_oid": merge_oid,
        "main_ancestry": {"verified": True, "reachable": ancestry},
        "scope_coverage": coverage,
    }
    evidence = {
        "schema": evidence_module.EVIDENCE_SCHEMA,
        "schema_version": 1,
        "target": {
            "repo": REPO,
            "issue_number": ISSUE_NUMBER,
            "body_sha256": evidence_module._body_digest(issue_body),
        },
        "freshness": {"status": "fresh"},
        "contradictory": False,
        "candidates": [candidate],
        "scope_coverage": None,
    }
    # #2699 P1-5 (Finding J / AC12): the disposition judgement itself runs
    # through the same production function `build_intake_capsule.py` calls,
    # not a verifier-local inline ternary.
    disposition_result = evidence_module.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE_NUMBER)
    legacy_compatibility_disposition = disposition_result["disposition"]
    payload.update(
        {
            "issue_identity": {
                "number": issue.get("number"),
                "url": issue.get("url"),
                "body_sha256": _digest(issue_body),
            },
            "pr_identity": {
                "number": pr.get("number"),
                "url": pr.get("url"),
                "state": pr.get("state"),
                "merge_oid": merge_oid,
            },
            "non_closing_relation": not closing,
            "merge_commit_current_main_ancestry": ancestry,
            "legacy_scope_coverage_marker_missing": legacy_marker_missing,
            "legacy_compatibility_disposition": legacy_compatibility_disposition,
            "legacy_compatibility_disposition_reason_codes": disposition_result.get("reason_codes", []),
            "compare_status": compare_out.strip(),
            "status": "PASS"
            if issue.get("number") == ISSUE_NUMBER
            and pr.get("number") == PR_NUMBER
            and not closing
            and ancestry
            and legacy_marker_missing
            and legacy_compatibility_disposition == "ordinary_dispatch_or_explicit_recovery"
            else "FAIL",
            "reason": None,
            "errors": [],
        }
    )
    _write_artifact(artifact_path, payload)
    return payload, 0 if payload["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--artifact-path", required=True)
    args = parser.parse_args()
    payload, code = verify(artifact_path=Path(args.artifact_path))
    print(f"{payload['status']}: {payload.get('reason') or 'live #2119 / PR #2137 metadata verified'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
