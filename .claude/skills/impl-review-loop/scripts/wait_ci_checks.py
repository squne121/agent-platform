#!/usr/bin/env python3
"""Wait for required CI checks for an impl-review-loop PR head SHA."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote

GH_ENV = {"GH_PROMPT_DISABLED": "1"}

EXIT_PASS = 0
EXIT_NEGATIVE = 1
EXIT_RUNTIME = 2


def run_gh(args: list[str]) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update(GH_ENV)
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", "gh not found"
    return result.returncode, result.stdout, result.stderr


def classify_gh_error(stderr: str, rc: int) -> str:
    lowered = stderr.lower()
    if rc == 127:
        return "gh_error"
    if any(token in lowered for token in ("authenticat", "bad credentials", "not logged in")):
        return "auth_error"
    if any(token in lowered for token in ("forbidden", "resource not accessible", "not authorized", "permission")):
        return "auth_error"
    return "gh_error"


def emit_result(
    *,
    status: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    current_head_sha: str,
    checks: list[dict[str, Any]],
    elapsed_seconds: int,
    interval_seconds: int,
    timeout_seconds: int,
    error_code: str | None,
    message: str | None,
    exit_code: int,
) -> int:
    payload = {
        "schema": "CI_WAIT_RESULT_V1",
        "status": status,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "current_head_sha": current_head_sha,
        "required_only": True,
        "checks": checks,
        "elapsed_seconds": elapsed_seconds,
        "interval_seconds": interval_seconds,
        "timeout_seconds": timeout_seconds,
        "error_code": error_code,
        "message": message,
    }
    print(f"CI_WAIT_RESULT_V1_JSON={json.dumps(payload, ensure_ascii=True)}")
    return exit_code


def get_current_head_sha(repo: str, pr_number: int) -> tuple[str | None, str | None, str | None]:
    rc, stdout, stderr = run_gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"]
    )
    if rc != 0:
        return None, classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    head_sha = stdout.strip()
    if not head_sha:
        return None, "malformed_gh_response", "empty headRefOid"
    return head_sha, None, None


_ZERO_ROW_STDERR_PHRASES = ("no required checks reported", "no checks reported")


def fetch_checks(repo: str, pr_number: int) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    fields = "name,bucket,state,workflow,link,startedAt,completedAt"
    rc, stdout, stderr = run_gh(
        ["pr", "checks", str(pr_number), "--repo", repo, "--required", "--json", fields]
    )
    if rc != 0:
        # `gh pr checks` reports a zero-row result as an error exit. That is the
        # normal "CI has not materialized yet" state, not a runtime failure.
        lowered = f"{stderr}\n{stdout}".lower()
        if any(phrase in lowered for phrase in _ZERO_ROW_STDERR_PHRASES):
            return [], None, None
        return None, classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "malformed_gh_response", stdout[:400]
    if not isinstance(data, list):
        return None, "malformed_gh_response", "gh pr checks did not return a list"
    return data, None, None


def get_base_branch(repo: str, pr_number: int) -> tuple[str | None, str | None, str | None]:
    rc, stdout, stderr = run_gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "baseRefName", "--jq", ".baseRefName"]
    )
    if rc != 0:
        return None, classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    base = stdout.strip()
    if not base:
        return None, "malformed_gh_response", "empty baseRefName"
    return base, None, None


# context name -> set of source app IDs (Ruleset integration_id / Classic app_id).
Inventory = dict[str, set[int | None]]


def _add_context(inventory: Inventory, context: Any, app_id: Any, *, record_app_id: bool = True) -> bool:
    if not isinstance(context, str) or not context:
        return False
    if app_id is not None and (isinstance(app_id, bool) or not isinstance(app_id, int)):
        return False
    ids = inventory.setdefault(context, set())
    if record_app_id:
        ids.add(app_id)  # None = source reported no app (e.g. Ruleset integration_id: null)
    return True


def _fetch_ruleset_contexts(
    repo: str, base_encoded: str, inventory: Inventory
) -> tuple[str | None, str | None]:
    # `rules/branches` is paginated (per_page max 100). `--paginate --slurp` makes gh read every
    # page and print one outer JSON array holding one array per page. Any page failure makes gh
    # exit non-zero, so a partial inventory is never accepted.
    rc, stdout, stderr = run_gh(
        ["api", f"repos/{repo}/rules/branches/{base_encoded}?per_page=100", "--paginate", "--slurp"]
    )
    if rc != 0:
        return classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    try:
        pages = json.loads(stdout)
    except json.JSONDecodeError:
        return "malformed_gh_response", stdout[:400]
    if not isinstance(pages, list):
        return "malformed_gh_response", "rules/branches --slurp did not return an array of pages"
    data: list[Any] = []
    for page in pages:
        if not isinstance(page, list):
            return "malformed_gh_response", "rules/branches page is not a list"
        data.extend(page)
    for rule in data:
        if not isinstance(rule, dict):
            return "malformed_gh_response", "rules/branches entry is not an object"
        rule_type = rule.get("type")
        if not isinstance(rule_type, str) or not rule_type:
            return "malformed_gh_response", "rules/branches entry has no valid type"
        if rule_type != "required_status_checks":
            continue  # Unknown but well-formed rule types are ignored (forward compatible).
        params = rule.get("parameters")
        entries = params.get("required_status_checks") if isinstance(params, dict) else None
        if not isinstance(entries, list):
            return "malformed_gh_response", "required_status_checks rule has unexpected shape"
        for entry in entries:
            if not isinstance(entry, dict) or not _add_context(
                inventory, entry.get("context"), entry.get("integration_id")
            ):
                return "malformed_gh_response", "required_status_checks entry has unexpected shape"
    return None, None


def _fetch_classic_contexts(
    repo: str, base_encoded: str, inventory: Inventory
) -> tuple[str | None, str | None]:
    rc, stdout, stderr = run_gh(
        ["api", f"repos/{repo}/branches/{base_encoded}/protection/required_status_checks"]
    )
    if rc != 0:
        if "branch not protected" in f"{stderr}\n{stdout}".lower():
            return None, None  # Classic protection absent: this source is empty.
        return classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "malformed_gh_response", stdout[:400]
    if not isinstance(data, dict):
        return "malformed_gh_response", "required_status_checks did not return an object"
    checks = data.get("checks", [])
    contexts = data.get("contexts", [])
    if not isinstance(checks, list) or not isinstance(contexts, list):
        return "malformed_gh_response", "required_status_checks has unexpected shape"
    for entry in checks:
        if not isinstance(entry, dict) or not _add_context(inventory, entry.get("context"), entry.get("app_id")):
            return "malformed_gh_response", "required_status_checks.checks entry has unexpected shape"
    for context in contexts:
        if not _add_context(inventory, context, None, record_app_id=False):
            return "malformed_gh_response", "required_status_checks.contexts entry has unexpected shape"
    return None, None


def fetch_required_inventory(repo: str, pr_number: int) -> tuple[Inventory | None, str | None, str | None]:
    """Read-only effective required-check inventory for the PR base branch.

    Union (by context name) of the active Ruleset required_status_checks and the
    Classic branch protection required_status_checks. Any failure of either source
    (other than Classic 404 "Branch not protected") fails closed.
    """
    base, error, message = get_base_branch(repo, pr_number)
    if error or base is None:
        return None, error, message
    base_encoded = quote(base, safe="")
    inventory: Inventory = {}
    for fetch in (_fetch_ruleset_contexts, _fetch_classic_contexts):
        error, message = fetch(repo, base_encoded, inventory)
        if error:
            return None, error, message
    return inventory, None, None


def decide_status(checks: list[dict[str, Any]]) -> tuple[str, str]:
    buckets = [check.get("bucket") for check in checks]
    if any(bucket == "pending" or bucket is None for bucket in buckets):
        return "pending", "required checks still pending"
    if any(bucket == "fail" for bucket in buckets):
        return "failed", "required checks failed"
    if any(bucket == "cancel" for bucket in buckets):
        return "cancelled", "required checks were cancelled"
    if any(bucket == "skipping" for bucket in buckets):
        if all(bucket == "skipping" for bucket in buckets):
            return "skipped_only", "all required checks are skipped"
        return "failed", "required checks include skipped entries"
    return "passed", "all required checks passed"


def _timeout_message(missing: list[str]) -> str:
    if missing:
        return "timeout waiting for required checks; missing required contexts: " + ", ".join(missing)
    return "timeout waiting for required checks"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for required PR checks to complete.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int, dest="pr_number")
    parser.add_argument("--head-sha", required=True, dest="head_sha")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=1800, dest="timeout_seconds")
    args = parser.parse_args(argv)
    if not args.required:
        parser.error("--required is mandatory for wait_ci_checks.sh")
    if args.interval <= 0 or args.timeout_seconds <= 0:
        parser.error("--interval and --timeout-seconds must be positive integers")
    return args


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_RUNTIME
        return emit_result(
            status="gh_error",
            repo="",
            pr_number=0,
            head_sha="",
            current_head_sha="",
            checks=[],
            elapsed_seconds=0,
            interval_seconds=0,
            timeout_seconds=0,
            error_code="invalid_args",
            message="invalid arguments",
            exit_code=EXIT_RUNTIME if code != 0 else EXIT_PASS,
        )

    if shutil_which("gh") is None:
        return emit_result(
            status="gh_error",
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=args.head_sha,
            checks=[],
            elapsed_seconds=0,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code="gh_error",
            message="gh CLI not found",
            exit_code=EXIT_RUNTIME,
        )

    start_ts = time.time()

    current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
    if head_error:
        return emit_result(
            status=head_error,
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha="",
            checks=[],
            elapsed_seconds=0,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code=head_error,
            message=head_message,
            exit_code=EXIT_RUNTIME,
        )
    if current_head_sha != args.head_sha:
        return emit_result(
            status="head_sha_changed",
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=current_head_sha or "",
            checks=[],
            elapsed_seconds=0,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code="head_sha_changed",
            message="head SHA changed before wait",
            exit_code=EXIT_NEGATIVE,
        )

    inventory, inventory_error, inventory_message = fetch_required_inventory(args.repo, args.pr_number)
    if inventory_error or inventory is None:
        return emit_result(
            status=inventory_error or "gh_error",
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=current_head_sha or "",
            checks=[],
            elapsed_seconds=int(time.time() - start_ts),
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code=inventory_error or "gh_error",
            message=inventory_message or "required-check inventory unavailable",
            exit_code=EXIT_RUNTIME,
        )
    last_missing: list[str] = sorted(inventory)

    while True:
        elapsed = int(time.time() - start_ts)
        if elapsed >= args.timeout_seconds:
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            if current_head_sha != args.head_sha:
                return emit_result(
                    status="head_sha_changed",
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha=current_head_sha or "",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code="head_sha_changed",
                    message="head SHA changed while waiting for checks",
                    exit_code=EXIT_NEGATIVE,
                )
            return emit_result(
                status="pending_timeout",
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=[],
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code="pending_timeout",
                message=_timeout_message(last_missing),
                exit_code=EXIT_NEGATIVE,
            )

        checks, checks_error, checks_message = fetch_checks(args.repo, args.pr_number)
        if checks_error:
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            return emit_result(
                status=checks_error,
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=[],
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code=checks_error,
                message=checks_message,
                exit_code=EXIT_RUNTIME,
            )

        if not checks and not inventory:
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            return emit_result(
                status="no_checks",
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=[],
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code="no_checks",
                message="required checks are not available",
                exit_code=EXIT_NEGATIVE,
            )

        if checks:
            decision, message = decide_status(checks)
        else:
            decision, message = "pending", "required checks not yet reported"
        materialized = {check.get("name") for check in checks}
        last_missing = sorted(context for context in inventory if context not in materialized)
        if last_missing and decision in ("passed", "skipped_only"):
            decision = "pending"
        if decision == "pending":
            # Issue #2836 F3: a pending poll must still detect HEAD drift so that a
            # stale SHA never waits out the full timeout as pending_timeout.
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=checks,
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            if current_head_sha != args.head_sha:
                return emit_result(
                    status="head_sha_changed",
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha=current_head_sha or "",
                    checks=checks,
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code="head_sha_changed",
                    message="head SHA changed while waiting for checks",
                    exit_code=EXIT_NEGATIVE,
                )
            time.sleep(args.interval)
            continue

        current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
        if head_error:
            return emit_result(
                status=head_error,
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha="",
                checks=checks,
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code=head_error,
                message=head_message,
                exit_code=EXIT_RUNTIME,
            )
        if current_head_sha != args.head_sha:
            return emit_result(
                status="head_sha_changed",
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=checks,
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code="head_sha_changed",
                message="head SHA changed while waiting for checks",
                exit_code=EXIT_NEGATIVE,
            )

        exit_code = EXIT_PASS if decision == "passed" else EXIT_NEGATIVE
        error_code = None if decision == "passed" else decision
        return emit_result(
            status=decision,
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=current_head_sha or "",
            checks=checks,
            elapsed_seconds=elapsed,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code=error_code,
            message=message,
            exit_code=exit_code,
        )


def shutil_which(name: str) -> str | None:
    from shutil import which
    return which(name)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
