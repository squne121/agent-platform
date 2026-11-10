#!/usr/bin/env python3
"""
ci_verdict_summary.py — CI checks を compact verdict に変換する CLI script。

PR 番号・expected head SHA・任意のチェック名を入力に CI checks を
CI_VERDICT_SUMMARY_V1 schema の compact JSON として stdout に出力する。

exit codes:
  0: all_pass
  10: failed / no_required_evidence
  20: pending_or_queued
  30: stale_head_sha
  40: gh_error

優先順位: stale_head_sha > gh_error > pending_or_queued > failed > all_pass
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Conclusion → bucket mapping
CONCLUSION_BUCKET: dict[str, str] = {
    "success": "pass",
    "failure": "fail",
    "timed_out": "fail",
    "cancelled": "cancel",
    "action_required": "fail",
    "neutral": "fail",   # approve evidence として不可
    "skipped": "fail",   # approve evidence として不可
    "stale": "fail",     # approve evidence として不可
}

# Status → pending indicator
PENDING_STATUSES: set[str] = {
    "queued",
    "in_progress",
    "waiting",
    "requested",
    "pending",
}

# Exit codes
EXIT_ALL_PASS = 0
EXIT_FAILED = 10
EXIT_NO_REQUIRED_EVIDENCE = 10  # same exit as failed — no required CI evidence
EXIT_PENDING = 20
EXIT_STALE = 30
EXIT_GH_ERROR = 40

# Allowlist: (workflow_name, check_name) tuples for head_sha=None + skipped exclusion.
# These checks are conditional/retrospective and are excluded from verdict calculation
# when they have head_sha=None and conclusion=skipped.
# workflow names correspond to .github/workflows/ file `name:` fields.
HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES: frozenset[tuple[str, str]] = frozenset({
    ("deploy-pages", "deploy-main"),
    ("deploy-pages", "cleanup-pr"),
    ("agent-retro-index", "build-index"),
    ("agent-retro-index", "upsert-parent-comment"),
    ("Check Japanese Content", "Issue Body Japanese Check (retrospective)"),
    ("Check Japanese Content", "Issue Comment Japanese Check (retrospective)"),
    ("Check Japanese Content", "PR Review Japanese Check (retrospective)"),
    ("retro-live-verification", "post-canonical-comment"),
})

# Issue #2524 AC12: trusted consumer (visual-impact-policy-trusted-consumer /
# visual-impact-policy-trusted) is always advisory in the V1 verdict path,
# matching ci_verdict_summary_v2.py's "excluded" classification for the
# same (workflow, check_name) tuple. Producer ("ci", "visual-impact-policy")
# is NOT in this set and remains fully blocking.
UNCONDITIONAL_EXCLUDE_RULES: frozenset[tuple[str, str]] = frozenset({
    ("visual-impact-policy-trusted-consumer", "visual-impact-policy-trusted"),
})

# Issue #2433 / PR #2561: these dispatch-only CI jobs are intentionally skipped
# on ordinary pull_request runs.  This narrowly scoped rule is deliberately
# stricter than UNCONDITIONAL_EXCLUDE_RULES: an exact, provenance-complete,
# current-head skipped CheckRun from the ordinary PR event is required.  A
# failure, any other event, stale provenance, incomplete direct detail, and
# every unknown tuple remain blocking.
ORDINARY_PR_SKIPPED_EXCLUDE_RULES: frozenset[tuple[str, str]] = frozenset({
    ("ci", "reliability-assessment"),
    ("ci", "close-evidence-publication"),
    ("ci", "ci-runtime-baseline-gate-ready"),
})

# Artifact truncation limit (bytes)
LOG_TRUNCATE_BYTES = 64 * 1024  # 64KB


def sanitize_check_name(name: str) -> str:
    """Path traversal 不可な safe ファイル名に変換する。"""
    # Replace path separators and dangerous chars
    safe = re.sub(r"[/\\:*?\"<>|]", "_", name)
    # Remove dot-dot sequences (path traversal prevention)
    safe = re.sub(r"\.\.+", "_", safe)
    # Remove leading dots (hidden files / relative path tricks)
    safe = re.sub(r"^\.+", "", safe)
    # Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe)
    # Trim to 128 chars
    safe = safe[:128].strip("_") or "unnamed"
    return safe


def classify_gh_error(stderr: str) -> str:
    """
    B6: 共通 gh エラー分類関数。
    fetch_head_sha, fetch_checks, fetch_run_details, fetch_job_log で統一使用する。
    """
    s = stderr.lower()
    if "unauthorized" in s or "authentication" in s or "credentials" in s:
        return "auth_failed"
    if "403" in s or "permission" in s or "forbidden" in s:
        return "permission_denied"
    if "rate limit" in s or "429" in s:
        return "rate_limited"
    if "404" in s or "not found" in s:
        return "not_found"
    if "json" in s or "parse" in s or "decode" in s:
        return "json_parse_error"
    return "gh_other_error"


def run_gh(args: list[str]) -> tuple[bool, Any, str]:
    """gh コマンドを実行し (success, parsed_json_or_None, raw_text) を返す。"""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return False, None, detail[:1024]
        text = result.stdout.strip()
        try:
            return True, json.loads(text), text
        except json.JSONDecodeError:
            return True, None, text
    except subprocess.TimeoutExpired:
        return False, None, "gh command timed out"
    except FileNotFoundError:
        return False, None, "gh not found in PATH"
    except Exception as e:
        return False, None, str(e)[:512]


def get_repo_root() -> Path:
    """B5: git rev-parse --show-toplevel で repo root を取得する。"""
    try:
        result = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return Path(result.strip())
    except Exception:
        # fallback to cwd if git is not available (e.g., in tests)
        return Path.cwd()


def fetch_head_sha(pr_number: int, repo: str) -> tuple[Optional[str], Optional[dict]]:
    """PR の headRefOid を取得する。失敗時は (None, error_entry)。"""
    ok, data, raw = run_gh([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "headRefOid",
    ])
    if not ok or data is None:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": kind, "detail": f"gh pr view headRefOid failed: {raw}"}
    head = data.get("headRefOid")
    if not head:
        return None, {"kind": "gh_other_error", "detail": "headRefOid missing in gh pr view output"}
    return head, None


STATUS_CHECK_ROLLUP_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              id
              contexts(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  __typename
                  ... on CheckRun {
                    databaseId
                    name
                    status
                    conclusion
                    startedAt
                    completedAt
                    detailsUrl
                    checkSuite {
                      commit { oid }
                      workflowRun { event workflow { name } }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _conclusion_bucket(conclusion: Optional[str], status: Optional[str]) -> Optional[str]:
    """GraphQL CheckRun conclusion/status を既存の内部 bucket に正規化する。"""
    normalized_conclusion = (conclusion or "").lower()
    normalized_status = (status or "").lower()
    if normalized_status != "completed":
        return "pending"
    if normalized_conclusion == "success":
        return "pass"
    if normalized_conclusion == "cancelled":
        return "cancel"
    if normalized_conclusion == "skipped":
        return "skipping"
    if normalized_conclusion:
        return "fail"
    return None


def fetch_checks(pr_number: int, repo: str) -> tuple[Optional[list], Optional[dict]]:
    """GraphQL statusCheckRollup の全 page から CheckRun provenance を取得する。"""
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        return None, {"kind": "json_parse_error", "detail": f"invalid repo: {repo}"}

    raw_runs: list[dict] = []
    cursor: Optional[str] = None
    seen_cursors: set[str] = set()
    root_commit_oid: Optional[str] = None
    rollup_id: Optional[str] = None
    try:
        while True:
            gh_args = [
                "api", "graphql",
                "-f", f"query={STATUS_CHECK_ROLLUP_QUERY}",
                "-F", f"owner={owner}",
                "-F", f"name={name}",
                "-F", f"number={pr_number}",
            ]
            if cursor is not None:
                # GraphQL cursor は opaque String。最初の request では変数自体を
                # 省略し、次 page 以降だけ raw-field で文字列のまま渡す。
                gh_args.extend(["-f", f"cursor={cursor}"])
            ok, data, raw = run_gh(gh_args)
            if not ok or data is None:
                return None, {"kind": classify_gh_error(raw), "detail": raw[:512]}

            commits = data["data"]["repository"]["pullRequest"]["commits"]["nodes"]
            commit = commits[-1]["commit"]
            page_commit_oid = commit["oid"]
            rollup = commit["statusCheckRollup"]
            if not isinstance(page_commit_oid, str) or not page_commit_oid:
                raise ValueError("statusCheckRollup commit.oid missing or invalid")
            if not isinstance(rollup, dict):
                raise ValueError("statusCheckRollup root must be an object")
            page_rollup_id = rollup["id"]
            if not isinstance(page_rollup_id, str) or not page_rollup_id:
                raise ValueError("statusCheckRollup id missing or invalid")
            if root_commit_oid is None:
                root_commit_oid = page_commit_oid
                rollup_id = page_rollup_id
            elif page_commit_oid != root_commit_oid or page_rollup_id != rollup_id:
                raise ValueError("statusCheckRollup root changed during pagination")

            contexts = rollup["contexts"]
            page_info = contexts["pageInfo"]
            has_next_page = page_info["hasNextPage"]
            page_runs = contexts["nodes"]
            if not isinstance(has_next_page, bool):
                raise ValueError("statusCheckRollup pageInfo.hasNextPage must be boolean")
            if not isinstance(page_runs, list):
                raise ValueError("statusCheckRollup contexts.nodes must be a list")
            raw_runs.extend(page_runs)
            if not has_next_page:
                break

            next_cursor = page_info["endCursor"]
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ValueError("statusCheckRollup pageInfo.endCursor missing or invalid")
            if next_cursor in seen_cursors:
                raise ValueError("statusCheckRollup pageInfo.endCursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return None, {"kind": "json_parse_error", "detail": f"statusCheckRollup shape: {exc}"}

    checks: list[dict] = []
    for run in raw_runs:
        if not isinstance(run, dict):
            return None, {
                "kind": "json_parse_error",
                "detail": "statusCheckRollup shape: context node must be an object",
            }
        if run.get("__typename") != "CheckRun":
            continue
        suite_raw = run.get("checkSuite")
        if suite_raw is not None and not isinstance(suite_raw, dict):
            return None, {
                "kind": "json_parse_error",
                "detail": "statusCheckRollup shape: CheckRun checkSuite must be an object or null",
            }
        suite = suite_raw or {}
        workflow_run_raw = suite.get("workflowRun")
        if workflow_run_raw is not None and not isinstance(workflow_run_raw, dict):
            return None, {
                "kind": "json_parse_error",
                "detail": "statusCheckRollup shape: CheckRun workflowRun must be an object or null",
            }
        workflow_run = workflow_run_raw or {}
        workflow_raw = workflow_run.get("workflow")
        if workflow_raw is not None and not isinstance(workflow_raw, dict):
            return None, {
                "kind": "json_parse_error",
                "detail": "statusCheckRollup shape: CheckRun workflow must be an object or null",
            }
        workflow = workflow_raw or {}
        commit_raw = suite.get("commit")
        if commit_raw is not None and not isinstance(commit_raw, dict):
            return None, {
                "kind": "json_parse_error",
                "detail": "statusCheckRollup shape: CheckRun commit must be an object or null",
            }
        suite_commit = commit_raw or {}
        checks.append({
            "name": run.get("name") or "unknown",
            "bucket": _conclusion_bucket(run.get("conclusion"), run.get("status")),
            "state": run.get("conclusion") or run.get("status"),
            "workflow": workflow.get("name"),
            "link": run.get("detailsUrl"),
            "event": workflow_run.get("event"),
            "startedAt": run.get("startedAt"),
            "completedAt": run.get("completedAt"),
            "runId": run.get("databaseId"),
            "headSha": suite_commit.get("oid"),
            "run_detail_complete": all(
                run.get(field) is not None
                for field in ("databaseId", "status", "conclusion", "startedAt", "completedAt")
            ) and bool(suite_commit.get("oid")),
        })
    return checks, None


def fetch_run_details(run_id: int, repo: str) -> tuple[Optional[dict], Optional[dict]]:
    """gh run view で run の詳細（headSha / conclusion / jobs）を取得する。"""
    ok, data, raw = run_gh([
        "run", "view", str(run_id),
        "--repo", repo,
        "--json", "headSha,conclusion,status,workflowName,jobs,databaseId",
    ])
    if not ok or data is None:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": kind, "detail": f"gh run view {run_id}: {raw}"}
    return data, None


def fetch_job_log(job_id: int, repo: str) -> tuple[Optional[str], Optional[dict]]:
    """gh run view --log でジョブログを取得する（raw text）。"""
    ok, _, raw = run_gh([
        "run", "view",
        "--repo", repo,
        "--job", str(job_id),
        "--log",
    ])
    if not ok:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": "log_fetch_error", "detail": f"gh run view --log job {job_id}: {raw}", "gh_kind": kind}
    return raw, None


def extract_run_id_from_link(link: str) -> Optional[int]:
    """gh pr checks の link フィールドから run_id を抽出する。"""
    if not link:
        return None
    # https://github.com/{owner}/{repo}/actions/runs/{run_id}
    m = re.search(r"/actions/runs/(\d+)", link)
    if m:
        return int(m.group(1))
    return None


def classify_check(check: dict, pr_head_sha: str) -> dict:
    """
    gh pr checks の1エントリを解析し、check_entry dict を返す。

    check は以下のキーを持つ（gh pr checks --json 出力）:
      bucket, name, state, workflow, link, event, startedAt, completedAt
    """
    name = check.get("name") or "unknown"
    bucket = check.get("bucket")          # pass | fail | pending | skipping | cancel | null
    state = check.get("state")            # gh pr checks の state フィールド（bucket 別名的）
    workflow = check.get("workflow") or None
    link = check.get("link") or None
    started_at = check.get("startedAt") or None
    completed_at = check.get("completedAt") or None

    # GraphQL CheckRun databaseId を優先し、旧形式 fixture では link から補う。
    run_id = check.get("runId") or (extract_run_id_from_link(link) if link else None)

    # bucket → conclusion / status mapping
    # gh pr checks の bucket: pass / fail / pending / skipping / cancel / null
    conclusion: Optional[str] = None
    status: Optional[str] = None
    check_head_sha: Optional[str] = None

    if bucket == "pass":
        conclusion = "success"
        status = "completed"
    elif bucket == "fail":
        conclusion = "failure"
        status = "completed"
    elif bucket == "cancel":
        conclusion = "cancelled"
        status = "completed"
    elif bucket == "skipping":
        conclusion = "skipped"
        status = "completed"
    elif bucket == "pending":
        status = "in_progress"
    # null bucket → unknown, will rely on run details

    entry: dict[str, Any] = {
        "name": name,
        "workflow": workflow,
        # event は cross-event shadow と同一 event の再実行を識別する内部 provenance。
        # CI_VERDICT_SUMMARY_V1 の公開 checks schema には出力しない。
        "event": check.get("event") or None,
        "state": state,
        "bucket": bucket,
        "status": status,
        "conclusion": conclusion,
        "head_sha": check_head_sha,
        "run_id": run_id,
        "job_id": None,
        "details_url": link,
        "started_at": started_at,
        "completed_at": completed_at,
        "run_detail_complete": bool(check.get("run_detail_complete")),
    }
    if check.get("headSha"):
        entry["_run_head_sha"] = check["headSha"]
    return entry


def _entry_run_head_sha(entry: dict) -> Optional[str]:
    """Return the run-detail SHA without replacing producer provenance."""
    return entry.get("_run_head_sha") or entry.get("head_sha")


def determine_check_verdict(entry: dict, pr_head_sha: str) -> str:
    """
    check entry から verdict bucket を決定する。
    returns: "all_pass" | "failed" | "pending_or_queued" | "stale_head_sha" | "excluded"
    """
    # Issue #2524 AC12: trusted consumer の (workflow, name) は他のどの状態評価よりも
    # 優先して無条件 excluded にする（V2 の CLASSIFICATION_MAP "excluded" と同じ優先順位）。
    if (entry.get("workflow") or "", entry.get("name") or "") in UNCONDITIONAL_EXCLUDE_RULES:
        return "excluded"

    # stale: head SHA mismatch
    run_head_sha = _entry_run_head_sha(entry)
    if run_head_sha and run_head_sha != pr_head_sha:
        return "stale_head_sha"

    bucket = entry.get("bucket")
    conclusion = entry.get("conclusion")
    status = entry.get("status")
    name = entry.get("name") or ""
    workflow = entry.get("workflow") or ""

    # Only the three exact workflow_dispatch-only jobs may be excluded, and
    # only for their intentional ordinary-PR skipped CheckRun at this head.
    # Do not grant this exemption to a failed/cancelled/pending check, stale
    # provenance, incomplete direct CheckRun detail, or a similarly named job.
    if (
        (workflow, name) in ORDINARY_PR_SKIPPED_EXCLUDE_RULES
        and entry.get("event") == "pull_request"
        and status == "completed"
        and conclusion == "skipped"
        and _entry_run_head_sha(entry) == pr_head_sha
        and entry.get("run_detail_complete") is True
    ):
        return "excluded"

    # Allowlist exclusion: head_sha=None + conclusion=skipped + (workflow, name) in rules
    # These are conditional/retrospective checks that do not run on PR commits.
    if entry.get("head_sha") is None and conclusion == "skipped":
        if (workflow, name) in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES:
            return "excluded"

    # Direct CheckRun provenance が不完全なら、成功として通過させない。
    # canonicalization でも残し、CI 完了待ちとして fail-closed に扱う。
    if entry.get("run_id") is not None and not entry.get("run_detail_complete"):
        return "pending_or_queued"

    # pending
    if bucket == "pending" or status in PENDING_STATUSES:
        return "pending_or_queued"

    # conclusion-based
    if conclusion == "success":
        return "all_pass"
    if conclusion in ("failure", "timed_out", "action_required", "neutral", "skipped", "stale"):
        return "failed"
    if conclusion == "cancelled":
        return "failed"

    # bucket fallback
    if bucket == "pass":
        return "all_pass"
    if bucket in ("fail", "cancel", "skipping"):
        return "failed"

    # null / unknown → treat as pending
    return "pending_or_queued"


REVIEW_SHADOW_TUPLE = ("Check Japanese Content", "PR Body Japanese Check")
REVIEW_SHADOW_EVENTS = frozenset({"pull_request_review", "pull_request_review_comment"})


def _completed_timestamp(entry: dict) -> Optional[datetime]:
    """completed_at、次に started_at を UTC timestamp として正規化する。"""
    for field in ("completed_at", "started_at"):
        raw = entry.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def canonicalize_true_reruns(entries: list[dict]) -> list[dict]:
    """同一 (workflow, name, event, head SHA) の完了済み再実行だけを集約する。

    event 不明、timestamp 不明/同値、または未完了 run が混在する場合は判断を
    恣意化しないため、既存の fail-closed 分類を維持する。
    """
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for entry in entries:
        workflow, name, event = entry.get("workflow"), entry.get("name"), entry.get("event")
        run_head_sha = _entry_run_head_sha(entry)
        if not all(isinstance(value, str) and value for value in (workflow, name, event, run_head_sha)):
            continue
        if not entry.get("run_detail_complete"):
            continue
        groups.setdefault((workflow, name, event, run_head_sha), []).append(entry)

    superseded_ids: set[int] = set()
    for group in groups.values():
        if len(group) < 2 or any(entry.get("status") != "completed" for entry in group):
            continue
        dated = [(entry, _completed_timestamp(entry)) for entry in group]
        if any(timestamp is None for _, timestamp in dated):
            continue
        timestamps = [timestamp for _, timestamp in dated]
        if len(set(timestamps)) != len(timestamps):
            continue
        latest = max(dated, key=lambda item: item[1])[0]
        superseded_ids.update(id(entry) for entry in group if entry is not latest)

    return [entry for entry in entries if id(entry) not in superseded_ids]


def is_review_skipped_shadow(entry: dict, entries: list[dict], pr_head_sha: str) -> bool:
    """限定 review-event skipped shadow を current-head success peer により除外する。"""
    if (entry.get("workflow"), entry.get("name")) != REVIEW_SHADOW_TUPLE:
        return False
    if entry.get("event") not in REVIEW_SHADOW_EVENTS:
        return False
    if entry.get("state") != "SKIPPED" or not entry.get("run_detail_complete"):
        return False

    return any(
        (peer.get("workflow"), peer.get("name")) == REVIEW_SHADOW_TUPLE
        and peer.get("event") == "pull_request"
        and peer.get("conclusion") == "success"
        and _entry_run_head_sha(peer) == pr_head_sha
        and peer.get("run_detail_complete")
        for peer in entries
    )


def precompute_verdicts(entries: list[dict], pr_head_sha: str) -> list[tuple[dict, str]]:
    """detail 補完後の two-pass 集約。overall と各 bucket はこの結果だけを消費する。"""
    canonical_entries = canonicalize_true_reruns(entries)
    return [
        (
            entry,
            "excluded"
            if is_review_skipped_shadow(entry, canonical_entries, pr_head_sha)
            else determine_check_verdict(entry, pr_head_sha),
        )
        for entry in canonical_entries
    ]


def public_check_entry(entry: dict) -> dict:
    """V1 公開 schema へ内部 provenance を露出しない。"""
    return {
        key: value
        for key, value in entry.items()
        if key not in {"event", "run_detail_complete", "_run_head_sha"}
    }


def compute_overall_status(verdicts: list[str]) -> str:
    """優先順位: stale_head_sha > gh_error > pending_or_queued > failed > all_pass > no_required_evidence"""
    if "stale_head_sha" in verdicts:
        return "stale_head_sha"
    if "gh_error" in verdicts:
        return "gh_error"
    if "pending_or_queued" in verdicts:
        return "pending_or_queued"
    if "failed" in verdicts:
        return "failed"

    # Filter out excluded verdicts to determine effective evidence
    effective = [v for v in verdicts if v != "excluded"]
    if not effective and verdicts:
        # All checks are excluded — no required CI evidence to confirm pass
        return "no_required_evidence"

    return "all_pass"


def next_action_for(status: str) -> str:
    mapping = {
        "all_pass": "none",
        "failed": "inspect_failed_log_artifacts",
        "no_required_evidence": "inspect_failed_log_artifacts",
        "pending_or_queued": "wait_for_ci",
        "stale_head_sha": "refresh_head_sha",
        "gh_error": "manual_review_gh_error",
    }
    return mapping.get(status, "manual_review_gh_error")


def save_log_artifact(
    log_text: str,
    pr_number: int,
    head_sha: str,
    check_name: str,
    job_id: Optional[int],
    artifacts_base: Path,
) -> dict:
    """ログを artifact ファイルに保存し log_artifacts エントリを返す。"""
    safe_name = sanitize_check_name(check_name)
    artifact_dir = artifacts_base / "ci-verdict" / f"pr-{pr_number}" / f"head-{head_sha}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{safe_name}.log"

    raw_bytes = log_text.encode("utf-8", errors="replace")
    truncated = len(raw_bytes) > LOG_TRUNCATE_BYTES
    content_bytes = raw_bytes[:LOG_TRUNCATE_BYTES] if truncated else raw_bytes

    artifact_path.write_bytes(content_bytes)

    sha256 = hashlib.sha256(content_bytes).hexdigest()

    # B5: path は repo-root 相対の文字列で返す
    try:
        repo_root = get_repo_root()
        relative_path = artifact_path.relative_to(repo_root)
        path_str = str(relative_path)
    except ValueError:
        # artifact_path が repo_root 配下でない場合は絶対パスを使用
        path_str = str(artifact_path)

    return {
        "check_name": check_name,
        "job_id": job_id,
        "path": path_str,
        "sha256": f"sha256:{sha256}",
        "bytes": len(content_bytes),
        "truncated": truncated,
    }


def find_failed_job_id(jobs: list[dict], check_name: Optional[str]) -> Optional[int]:
    """
    B3: jobs リストから適切な job_id を取得する。
    jobs[0] を盲目的に使わず、failed job（conclusion != success）を優先する。
    check_name との対応が取れる場合はそれを優先する。
    """
    if not jobs:
        return None

    # まず check_name と一致する job を探す
    if check_name:
        for job in jobs:
            job_name = job.get("name") or ""
            if job_name == check_name or check_name in job_name:
                return job.get("databaseId")

    # 次に failed job を探す
    for job in jobs:
        conclusion = job.get("conclusion") or ""
        if conclusion and conclusion != "success":
            return job.get("databaseId")

    # fallback: first job
    return jobs[0].get("databaseId") if jobs else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CI checks を CI_VERDICT_SUMMARY_V1 compact JSON に変換する"
    )
    parser.add_argument("--pr", type=int, required=True, help="PR 番号")
    parser.add_argument("--repo", type=str, required=True, help="owner/repo")
    parser.add_argument("--expected-head-sha", type=str, required=True, help="期待する HEAD SHA")
    parser.add_argument("--check-name", type=str, default=None, help="exact match フィルタ")
    parser.add_argument(
        "--include-log-excerpt",
        action="store_true",
        help="artifact materialization のみ（stdout に raw log 出力禁止）",
    )

    args = parser.parse_args()

    pr_number: int = args.pr
    repo: str = args.repo
    expected_head_sha: str = args.expected_head_sha
    check_name_filter: Optional[str] = args.check_name
    include_log_excerpt: bool = args.include_log_excerpt

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    errors: list[dict] = []
    log_artifacts: list[dict] = []

    # Step 1: PR headRefOid を確定
    head_sha, head_err = fetch_head_sha(pr_number, repo)
    if head_err:
        errors.append(head_err)

    # stale check: expected vs actual head
    stale_from_head = False
    if head_sha and head_sha != expected_head_sha:
        stale_from_head = True

    # Step 2: GraphQL statusCheckRollup（gh pr checks の CLI 集約を使わない）
    raw_checks, checks_err = [], None
    if head_sha is not None:
        raw_checks, checks_err = fetch_checks(pr_number, repo)
        if checks_err:
            errors.append(checks_err)
            raw_checks = []
    elif not errors:
        # head_sha missing but no error recorded — shouldn't happen, safeguard
        errors.append({"kind": "gh_other_error", "detail": "head_sha unavailable"})

    # Step 3: classify checks
    check_entries: list[dict] = []
    verdicts: list[str] = []

    if stale_from_head:
        verdicts.append("stale_head_sha")

    if errors:
        verdicts.append("gh_error")

    # B1: 0件は gh_error。複数件は event-aware canonicalization 後に判定する。
    if check_name_filter and raw_checks is not None:
        matched = [raw for raw in raw_checks if (raw.get("name") or "unknown") == check_name_filter]
        if len(matched) == 0:
            errors.append({"kind": "check_not_found", "detail": f"check-name not found: {check_name_filter}"})
            raw_checks = []
        else:
            raw_checks = matched

    for raw in (raw_checks or []):
        entry = classify_check(raw, head_sha or expected_head_sha)
        # GraphQL の CheckRun は check-run ID と status/conclusion/head SHA を同時取得する。
        # head SHA 欠落・未完の detail は rerun 集約対象から外れ、後段で fail-closed に残る。
        if entry.get("run_id") is not None:
            entry["job_id"] = entry["run_id"]

        check_entries.append(entry)

    # review shadow の success peer は、現在の実 head だけでなく caller が固定した
    # expected head と一致して初めて有効とする。head drift 中に shadow を除外しない。
    verdict_pairs = precompute_verdicts(check_entries, expected_head_sha)
    check_entries = [entry for entry, _ in verdict_pairs]
    verdict_by_entry_id = {id(entry): verdict for entry, verdict in verdict_pairs}

    # cross-event の canonical success + 限定 shadow は正当な二重行として許可する。
    if check_name_filter and len(check_entries) > 1:
        allowed_shadow_pair = (
            len(check_entries) == 2
            and any(verdict == "excluded" for _, verdict in verdict_pairs)
            and any(
                entry.get("event") == "pull_request"
                and entry.get("conclusion") == "success"
                and _entry_run_head_sha(entry) == expected_head_sha
                for entry in check_entries
            )
        )
        if not allowed_shadow_pair:
            errors.append({
                "kind": "ambiguous_check_name",
                "detail": f"check-name matched multiple canonical checks: {check_name_filter}",
            })

        # log artifact: failed check のみ、--include-log-excerpt 指定時
    for entry in check_entries:
        verdict = verdict_by_entry_id[id(entry)]
        if include_log_excerpt and verdict == "failed" and entry.get("job_id"):
            log_text, log_err = fetch_job_log(entry["job_id"], repo)
            if log_err:
                # B4: ログ取得失敗時は errors に記録し next_action を manual_review_gh_error にする
                errors.append(log_err)
                verdicts.append("gh_error")
            elif log_text:
                # B5: repo root から絶対パスで artifacts ディレクトリを作成
                repo_root = get_repo_root()
                artifacts_base = repo_root / "artifacts"
                artifacts_base.mkdir(exist_ok=True)
                artifact_entry = save_log_artifact(
                    log_text,
                    pr_number,
                    head_sha or expected_head_sha,
                    entry["name"],
                    entry["job_id"],
                    artifacts_base,
                )
                log_artifacts.append(artifact_entry)

    if errors:
        verdicts.append("gh_error")
    verdicts.extend(verdict for _, verdict in verdict_pairs)

    # Overall status
    if not verdicts:
        # No checks found (check_name_filter が一致しない場合は B1 で gh_error 処理済み)
        # verdicts が空 = フィルタなしで required checks も 0 件 → 証跡なし →
        # no_required_evidence（Issue #1856 AC11: 0 件を all_pass にする
        # fail-open を解消。required checks が存在しないことは approve evidence
        # にならない）。
        overall_status = "no_required_evidence"
    else:
        overall_status = compute_overall_status(verdicts)

    # Build categorized lists
    failed_checks = [e["name"] for e in check_entries if verdict_by_entry_id[id(e)] == "failed"]
    pending_checks = [e["name"] for e in check_entries if verdict_by_entry_id[id(e)] == "pending_or_queued"]
    stale_checks = [e["name"] for e in check_entries if verdict_by_entry_id[id(e)] == "stale_head_sha"]
    excluded_checks = [e["name"] for e in check_entries if verdict_by_entry_id[id(e)] == "excluded"]

    summary: dict[str, Any] = {
        "schema": "CI_VERDICT_SUMMARY_V1",
        "generated_at": generated_at,
        "repo": repo,
        "pr": pr_number,
        "expected_head_sha": expected_head_sha,
        "head_sha": head_sha or "",
        "status": overall_status,
        "checks": [public_check_entry(entry) for entry in check_entries],
        "failed_checks": failed_checks,
        "pending_checks": pending_checks,
        "stale_checks": stale_checks,
        "excluded_checks": excluded_checks,
        "excluded_count": len(excluded_checks),
        "log_artifacts": log_artifacts,
        "errors": errors,
        "next_action": next_action_for(overall_status),
    }

    print(json.dumps(summary, indent=2, ensure_ascii=False))

    # exit code
    exit_map = {
        "all_pass": EXIT_ALL_PASS,
        "failed": EXIT_FAILED,
        "no_required_evidence": EXIT_NO_REQUIRED_EVIDENCE,
        "pending_or_queued": EXIT_PENDING,
        "stale_head_sha": EXIT_STALE,
        "gh_error": EXIT_GH_ERROR,
    }
    return exit_map.get(overall_status, EXIT_GH_ERROR)


if __name__ == "__main__":
    sys.exit(main())
