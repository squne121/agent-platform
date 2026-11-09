#!/usr/bin/env python3
"""CI failed log retrieval helper for impl-review-loop.

Selects the GitHub Actions workflow run matching the requested head SHA
(or --run-id when provided), retrieves failed job logs via gh CLI (primary)
or REST API (fallback), applies ANSI strip / token redaction / truncation,
and emits CI_FAILED_LOG_RESULT_V1_JSON at stdout end.
"""

import argparse
import json
import re
import subprocess
import sys
from typing import Optional

DEFAULT_MAX_BYTES = 60_000
SUBPROCESS_TIMEOUT = 60

PENDING_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}

GH_ENV = {"GH_PROMPT_DISABLED": "1"}


def run_gh(*args: str) -> tuple[int, str, str]:
    import os
    env = os.environ.copy()
    env.update(GH_ENV)
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "gh not found"


def classify_gh_error(stderr: str, rc: int) -> str:
    s = stderr.lower()
    if rc == 127:
        return "gh_error"
    if "401" in s or "authentication" in s or "credentials" in s or "not logged in" in s:
        return "auth_error"
    if "403" in s or "permission" in s or "forbidden" in s:
        return "permission_denied"
    if "timeout" in s or rc == 124:
        return "gh_error"
    return "gh_error"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", text)


KNOWN_TOKEN_PATTERN = re.compile(
    r"(?i)("
    r"ghp_[A-Za-z0-9]{36,}"
    r"|ghs_[A-Za-z0-9._-]{36,}"
    r"|github_pat_[A-Za-z0-9_]{59,}"
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
    r")"
)


def redact_tokens(text: str) -> tuple[str, bool]:
    redacted, n = KNOWN_TOKEN_PATTERN.subn("[REDACTED]", text)
    return redacted, n > 0


def truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n...[TRUNCATED]", True


def select_run(
    runs: list[dict],
    head_sha: str,
    workflow_filter: Optional[str],
) -> tuple[Optional[dict], str]:
    """Return (run, status_hint). status_hint is 'ok' or 'ambiguous'."""
    matching = [r for r in runs if r.get("headSha") == head_sha]
    if workflow_filter:
        matching = [r for r in matching if r.get("workflowName", "") == workflow_filter]
    if not matching:
        return None, "no_match"
    # Group by workflowName; if more than one distinct workflow matches without filter, warn
    names = {r.get("workflowName") for r in matching}
    if len(names) > 1 and not workflow_filter:
        return None, "ambiguous"
    matching.sort(key=lambda r: r.get("attempt", 1), reverse=True)
    return matching[0], "ok"


def get_failed_jobs(repo: str, run_id: int, attempt: int) -> list[dict]:
    """Fetch failed-like jobs from the jobs API with pagination."""
    rc, out, _ = run_gh(
        "api",
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        "--paginate",
        "--jq",
        ".jobs[]",
    )
    if rc != 0 or not out.strip():
        return []
    jobs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
            if job.get("conclusion") in FAILED_CONCLUSIONS or job.get("conclusion") is None:
                jobs.append({"id": job["id"], "name": job["name"], "conclusion": job.get("conclusion")})
        except (json.JSONDecodeError, KeyError):
            continue
    return jobs


def fetch_logs_primary(repo: str, run_id: int, attempt: int) -> tuple[str, bool]:
    rc, stdout, _ = run_gh(
        "run", "view", str(run_id),
        "--repo", repo,
        f"--attempt={attempt}",
        "--log-failed",
    )
    if rc == 0 and stdout.strip():
        return stdout, True
    return "", False


def fetch_logs_rest_fallback(repo: str, failed_jobs: list[dict]) -> tuple[str, bool]:
    all_logs: list[str] = []
    for job in failed_jobs:
        job_id = job["id"]
        rc2, log_out, _ = run_gh("api", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        if rc2 == 0 and log_out.strip():
            all_logs.append(f"=== {job['name']} ===\n{log_out}")
    combined = "\n".join(all_logs)
    return combined, bool(all_logs)


def emit_result(
    *,
    status: str,
    run_id: Optional[int],
    attempt: Optional[int],
    head_sha: str,
    workflow_name: Optional[str],
    failed_jobs: list[dict],
    retrieval_method: Optional[str],
    redaction_applied: bool,
    truncated: bool,
    log_text: str,
    exit_code: int,
) -> None:
    if log_text:
        print(log_text)
    marker = {
        "schema": "CI_FAILED_LOG_RESULT_V1",
        "status": status,
        "run_id": run_id,
        "attempt": attempt,
        "head_sha": head_sha,
        "workflow_name": workflow_name,
        "failed_jobs": failed_jobs,
        "retrieval_method": retrieval_method,
        "redaction_applied": redaction_applied,
        "truncated": truncated,
    }
    print(f"CI_FAILED_LOG_RESULT_V1_JSON: {json.dumps(marker)}")
    sys.exit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieve CI failed logs for a PR head SHA.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True, dest="head_sha")
    parser.add_argument("--run-id", type=int, default=None, dest="run_id")
    parser.add_argument("--workflow", default=None)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, dest="max_bytes")
    args = parser.parse_args()

    # When --run-id is provided, skip run selection and use directly
    if args.run_id:
        rc, out, err = run_gh(
            "run", "view", str(args.run_id),
            "--repo", args.repo,
            "--json", "databaseId,attempt,status,conclusion,headSha,workflowName",
        )
        if rc != 0:
            status_code = classify_gh_error(err, rc)
            emit_result(
                status=status_code, run_id=args.run_id, attempt=None,
                head_sha=args.head_sha, workflow_name=None, failed_jobs=[],
                retrieval_method=None, redaction_applied=False, truncated=False,
                log_text=f"gh run view failed: {err.strip()}", exit_code=2,
            )
        run = json.loads(out)
        if run.get("headSha") != args.head_sha:
            emit_result(
                status="head_sha_mismatch", run_id=args.run_id, attempt=None,
                head_sha=args.head_sha, workflow_name=run.get("workflowName"),
                failed_jobs=[], retrieval_method=None, redaction_applied=False,
                truncated=False, log_text="Provided --run-id does not match --head-sha.",
                exit_code=1,
            )
        run_id = args.run_id
        attempt = run.get("attempt", 1)
        workflow_name = run.get("workflowName", "")
        status_raw = run.get("status", "")
        conclusion = run.get("conclusion") or ""
    else:
        rc, out, err = run_gh(
            "run", "list",
            "--repo", args.repo,
            "--commit", args.head_sha,
            "--json", "databaseId,attempt,status,conclusion,headSha,workflowName,event,createdAt,updatedAt,url",
            "--limit", "50",
        )

        if rc != 0:
            status_code = classify_gh_error(err, rc)
            emit_result(
                status=status_code, run_id=None, attempt=None,
                head_sha=args.head_sha, workflow_name=None, failed_jobs=[],
                retrieval_method=None, redaction_applied=False, truncated=False,
                log_text=f"gh run list failed: {err.strip()}", exit_code=2,
            )

        try:
            runs = json.loads(out)
        except json.JSONDecodeError:
            emit_result(
                status="malformed_gh_response", run_id=None, attempt=None,
                head_sha=args.head_sha, workflow_name=None, failed_jobs=[],
                retrieval_method=None, redaction_applied=False, truncated=False,
                log_text=f"Failed to parse gh output: {out[:200]}", exit_code=2,
            )

        run, hint = select_run(runs, args.head_sha, args.workflow)
        if hint == "ambiguous":
            names = sorted({r.get("workflowName", "") for r in runs if r.get("headSha") == args.head_sha})
            emit_result(
                status="ambiguous_run", run_id=None, attempt=None,
                head_sha=args.head_sha, workflow_name=None, failed_jobs=[],
                retrieval_method=None, redaction_applied=False, truncated=False,
                log_text=f"Multiple workflows match head SHA. Specify --workflow or --run-id. Found: {names}",
                exit_code=1,
            )
        if run is None:
            emit_result(
                status="no_matching_run", run_id=None, attempt=None,
                head_sha=args.head_sha, workflow_name=None, failed_jobs=[],
                retrieval_method=None, redaction_applied=False, truncated=False,
                log_text="No run found matching the requested head SHA.", exit_code=1,
            )

        run_id = run["databaseId"]
        attempt = run.get("attempt", 1)
        workflow_name = run.get("workflowName", "")
        status_raw = run.get("status", "")
        conclusion = run.get("conclusion") or ""

    if status_raw in PENDING_STATUSES:
        emit_result(
            status="ci_pending", run_id=run_id, attempt=attempt,
            head_sha=args.head_sha, workflow_name=workflow_name, failed_jobs=[],
            retrieval_method=None, redaction_applied=False, truncated=False,
            log_text=f"Run {run_id} is {status_raw}; log not yet available.", exit_code=0,
        )

    if conclusion == "success":
        emit_result(
            status="ci_passed", run_id=run_id, attempt=attempt,
            head_sha=args.head_sha, workflow_name=workflow_name, failed_jobs=[],
            retrieval_method=None, redaction_applied=False, truncated=False,
            log_text="", exit_code=0,
        )

    # Always fetch failed_jobs regardless of retrieval method
    failed_jobs = get_failed_jobs(args.repo, run_id, attempt)

    log_text, primary_ok = fetch_logs_primary(args.repo, run_id, attempt)
    retrieval_method = "gh_log_failed" if primary_ok else None

    if not primary_ok:
        log_text, fallback_ok = fetch_logs_rest_fallback(args.repo, failed_jobs)
        if fallback_ok:
            retrieval_method = "rest_job_logs"
        else:
            emit_result(
                status="log_unavailable", run_id=run_id, attempt=attempt,
                head_sha=args.head_sha, workflow_name=workflow_name,
                failed_jobs=failed_jobs, retrieval_method="none",
                redaction_applied=False, truncated=False,
                log_text="Failed to retrieve logs via both primary and REST fallback.",
                exit_code=1,
            )

    log_text = strip_ansi(log_text)
    log_text, redaction_applied = redact_tokens(log_text)
    log_text, truncated = truncate(log_text, args.max_bytes)

    emit_result(
        status="ci_failed", run_id=run_id, attempt=attempt,
        head_sha=args.head_sha, workflow_name=workflow_name,
        failed_jobs=failed_jobs, retrieval_method=retrieval_method,
        redaction_applied=redaction_applied, truncated=truncated,
        log_text=log_text, exit_code=1,
    )


if __name__ == "__main__":
    main()
