#!/usr/bin/env python3
"""Transaction helper for deterministic issue creation.

Performs a single create-issue transaction for create-issue skill automation:
1) dedupe search
2) create issue
3) apply labels
4) register as sub-issue (optional)
5) register dependencies (optional, both --blocked-by and --blocking directions)
6) read-back verification (direction-specific, strict set match)
7) post partial-failure audit comment (if needed)

Hard-predecessor producer/caller inventory (AC7, #1946):
As of this Issue, no script in this repository (materialize_child_issues.py,
plan_child_materialization.py, check_issue_overlap.py, milestone_rollup.py) programmatically
creates a new issue while marking it as a hard predecessor (blocking direction) of an
existing issue. materialize_child_issues.py maps `depends_on` to `--dependency`
(the new child issue IS blocked-by an existing issue), which is the opposite semantic
direction and was already correct before this Issue. The producer of the #1943/#1842
reversal bug was a direct/manual `create_issue_txn.py` CLI invocation guided by
`SKILL.md`, not an automated caller script. Therefore AC7(a) applies: the fix is wired
entirely within this file's/SKILL.md's Allowed Paths (the new --blocking flag plus the
SKILL.md guidance below), and no follow-up hard-successor Issue is required for external
caller wiring at this time.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from typing import Any, Literal

# Stage 2-5 (Issue #35): the trusted repository slug comes from repo_identity.py only
# (config/repository_identity.v1.json); this module holds no slug constant of its own.
AGENT_GUARDS_IMPORT_ROOT = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"

# Default subprocess timeout (seconds) for `gh` invocations issued via
# run_command(). Salvaged from the Issue #2259 bridge server's
# `subprocess.run(..., timeout=180)` handling (PR #2286) and adapted to the
# single native path (Issue #2299 AC4): a hung `gh` call must fail closed with
# a TransactionError("timeout", ...) instead of blocking the transaction
# indefinitely.
_DEFAULT_GH_COMMAND_TIMEOUT_SECONDS = 180


# ---------------------------------------------------------------------------
# Parent resolution types (Blocker 1 tri-state)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParentResolution:
    """Tri-state result of parent resolution from issue body.

    state:
      "absent"        — no parent_issue field found in body (treat like no input)
      "explicit_none" — parent_issue declared as none/null/N/A/0 (no parent wanted)
      "number"        — parent_issue declared as a positive integer
    value: set only when state == "number"
    """

    state: Literal["absent", "explicit_none", "number"]
    value: int | None = None  # state=="number" のときのみセット


@dataclass(frozen=True)
class ParentCandidate:
    """A single parent-issue reference extracted from an issue body."""

    source: str  # "machine_contract" | "parent_heading" | "shorthand"
    raw: str
    value: int | None  # explicit none/null is None


# ---------------------------------------------------------------------------
# HTTP status extraction helper (Blocker 3)
# ---------------------------------------------------------------------------

_HTTP_STATUS_RE = re.compile(r"(?im)^HTTP/\S+\s+(\d{3})\b")


def _extract_http_status(text: str) -> int | None:
    """Return the last HTTP status code found in text (from --include output).

    Uses the last match to handle redirect chains (e.g., 301 -> 201) where the
    final response status is the authoritative one.
    """
    matches = list(_HTTP_STATUS_RE.finditer(text))
    return int(matches[-1].group(1)) if matches else None


class TransactionError(Exception):
    def __init__(self, stage: str, message: str, *, command: str | None = None, output: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.command = command
        self.output = output


@dataclass
class TransactionResult:
    status: str
    issue_number: int | None
    issue_url: str | None
    completed_steps: list[str]
    failure_stage: str | None = None
    failure_message: str | None = None
    dedupe_number: int | None = None
    parent_verified: bool | None = None
    # dependency_verified represents the --blocked-by/--dependency direction (kept name
    # for backward compatibility). blocking_verified is the new --blocking direction
    # result (AC5: TransactionResult exposes direction-specific verification outcomes).
    dependency_verified: bool | None = None
    blocking_verified: bool | None = None
    applied_steps: list[str] = field(default_factory=list)
    verified_steps: list[str] = field(default_factory=list)
    failed_readbacks: list[dict] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    audit_comment_posted: bool | None = None
    # Authoritative post-create readback fields (salvaged from Issue #2259/
    # PR #2286 AC7, Issue #2299 AC4). Populated by
    # run_transaction_with_authoritative_readback(); left None for callers
    # that invoke run_transaction() directly (unchanged, backward compatible).
    node_id: str | None = None
    body_sha256: str | None = None


def run_command(
    command: list[str],
    *,
    check: bool = False,
    capture_output: bool = True,
    timeout: float | None = _DEFAULT_GH_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            check=check,
            capture_output=capture_output,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransactionError(
            "timeout",
            f"command timed out after {timeout}s: {' '.join(command)}",
            command=" ".join(command),
            output=str(exc),
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    if argv is None:
        argv = sys.argv[1:]
    # Reconcile subcommand: argv[0] == "reconcile" routes to a separate parser.
    # This preserves full backward compatibility with the existing --repo/--title form.
    if argv and argv[0] == "reconcile":
        recon_parser = argparse.ArgumentParser(description="Reconcile label readback partial failure")
        recon_parser.add_argument("--repo", required=True, help="owner/repo")
        recon_parser.add_argument("--issue", type=int, required=True, help="issue number to reconcile")
        recon_parser.add_argument("--label", action="append", default=[], dest="label")
        recon_parser.add_argument("--parent-issue", type=int, default=0, dest="parent_issue")
        # --blocked-by: canonical flag name. --dependency remains as a legacy alias absorbed
        # into the same destination for full backward compatibility.
        recon_parser.add_argument("--dependency", action="append", type=int, default=[], dest="dependency")
        recon_parser.add_argument("--blocked-by", action="append", type=int, dest="dependency")
        # --blocking: new issue (or, in reconcile, the target issue) BLOCKS the given issue
        # number(s) (hard-predecessor direction). Kept as a separate destination from
        # --blocked-by/--dependency on purpose: a single generic "direction" flag would
        # reintroduce the same reversal bug this Issue fixes (see #1946 Notes for Reviewer).
        recon_parser.add_argument("--blocking", action="append", type=int, default=[], dest="blocking")
        recon_parser.add_argument("--gh", default="gh")
        ns = recon_parser.parse_args(argv[1:])
        ns.subcommand = "reconcile"
        return ns
    # Existing create parser (unchanged)
    parser = argparse.ArgumentParser(description="Create issue transaction helper")
    parser.add_argument("--repo", required=True, help="owner/repo or full repo path")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--issue-kind", default="", help="issue kind: implementation | research | parent | (empty)")
    parser.add_argument("--parent-issue", type=int, default=0)
    # --blocked-by: canonical flag name for "the new issue is blocked by <N>".
    # --dependency is a legacy alias absorbed into the same destination.
    parser.add_argument("--dependency", action="append", dest="dependency", type=int, default=[])
    parser.add_argument("--blocked-by", action="append", dest="dependency", type=int)
    # --blocking: the new issue BLOCKS <N> (hard-predecessor direction). Separate
    # destination from --blocked-by/--dependency by design (see reconcile parser comment
    # above and Notes for Reviewer in #1946: a shared direction= flag reintroduces the bug).
    parser.add_argument("--blocking", action="append", dest="blocking", type=int, default=[])
    parser.add_argument("--gh", default="gh")
    parser.add_argument(
        "--label-profile",
        dest="label_profile",
        default="standard",
        choices=["standard", "triage_only"],
        help="label profile: standard | triage_only",
    )
    # Opt-in bypass of this script's own internal title-only OPEN-issue dedupe search
    # (Issue #2602 AC2(g)). Default False preserves full backward compatibility for all
    # existing callers. Intended only for callers that already confirmed via an outer
    # dedupe_key search that issue creation is warranted (see run_transaction()'s
    # skip_internal_title_dedupe docstring comment above).
    parser.add_argument(
        "--skip-internal-title-dedupe",
        dest="skip_internal_title_dedupe",
        action="store_true",
        default=False,
    )
    ns = parser.parse_args(argv)
    ns.subcommand = "create"
    return ns


def _resolve_labels(
    labels: list[str], issue_kind: str, label_profile: str = "standard"
) -> list[str]:
    """Return the effective label list, prepending standard labels for implementation kind.

    Standard labels (_IMPLEMENTATION_STANDARD_LABELS) are merged at the front of the
    explicit label list when issue_kind == "implementation". Duplicates are deduplicated
    while preserving order (standard labels first, then caller-supplied extras).
    For any other issue_kind (or empty string), labels are returned unchanged.
    """
    if label_profile == "triage_only":
        # Triage-only profile: do not auto-assign the full implementation label set;
        # only ensure the triage-required signal is present (delivery-rollup auto path).
        triage_merged: list[str] = ["triage-required"]
        for label in labels:
            if label not in triage_merged:
                triage_merged.append(label)
        return triage_merged
    if issue_kind != "implementation":
        return list(labels)
    merged: list[str] = list(_IMPLEMENTATION_STANDARD_LABELS)
    for label in labels:
        if label not in merged:
            merged.append(label)
    return merged


def _normalize_dependency_numbers(
    dependency_issue_numbers: list[int | str], *, label: str = "--dependency"
) -> list[int]:
    """Parse and reject invalid dependency issue numbers before any mutation (AC3).

    Rejects (raising TransactionError stage="dependency-parse"):
      - booleans (bool is an int subclass in Python; True/False are not valid issue numbers)
      - cross-repository issue URLs/refs (values containing "/" or an http(s) scheme)
      - values that do not parse as an integer
      - non-positive integers (0 or negative)
    """
    normalized: list[int] = []
    for value in dependency_issue_numbers:
        if isinstance(value, bool):
            raise TransactionError(
                stage="dependency-parse",
                message=f"{label} issue number must not be a boolean",
                output=repr(value),
            )
        if isinstance(value, str) and ("/" in value or value.strip().lower().startswith("http")):
            raise TransactionError(
                stage="dependency-parse",
                message=(
                    f"{label} does not accept cross-repository issue URLs/refs; "
                    "use a same-repository issue number"
                ),
                output=value,
            )
        try:
            normalized_value = int(value)
        except (TypeError, ValueError) as exc:
            raise TransactionError(
                stage="dependency-parse",
                message=f"invalid {label} issue number",
                output=str(value),
            ) from exc
        if normalized_value <= 0:
            raise TransactionError(
                stage="dependency-parse",
                message=f"{label} issue number must be a positive integer",
                output=str(value),
            )
        normalized.append(normalized_value)
    return normalized


def _validate_dependency_directions(
    blocked_by_issue_numbers: list[int],
    blocking_issue_numbers: list[int],
    *,
    self_issue_number: int | None = None,
) -> None:
    """Validate blocked-by/blocking issue-number lists before any GraphQL mutation (AC3).

    Raises TransactionError(stage="dependency-validate", ...) for:
      - duplicate issue numbers within a single direction
      - the same issue number specified in both --blocked-by and --blocking
      - self_issue_number (the new issue, dedupe target, or reconcile target) referenced
        in either direction
    """
    if len(blocked_by_issue_numbers) != len(set(blocked_by_issue_numbers)):
        raise TransactionError(
            stage="dependency-validate",
            message="--blocked-by contains duplicate issue numbers",
            output=str(blocked_by_issue_numbers),
        )
    if len(blocking_issue_numbers) != len(set(blocking_issue_numbers)):
        raise TransactionError(
            stage="dependency-validate",
            message="--blocking contains duplicate issue numbers",
            output=str(blocking_issue_numbers),
        )
    overlap = set(blocked_by_issue_numbers) & set(blocking_issue_numbers)
    if overlap:
        raise TransactionError(
            stage="dependency-validate",
            message=(
                "issue number(s) specified in both --blocked-by and --blocking: "
                f"{sorted(overlap)}"
            ),
            output=str(sorted(overlap)),
        )
    if self_issue_number is not None:
        if self_issue_number in blocked_by_issue_numbers or self_issue_number in blocking_issue_numbers:
            raise TransactionError(
                stage="dependency-validate",
                message=(
                    f"issue #{self_issue_number} cannot reference itself in "
                    "--blocked-by/--blocking"
                ),
                output=str(self_issue_number),
            )


def _github_owner_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise ValueError("--repo must be <owner>/<name>")
    owner, name = repo.split("/", 1)
    if not owner or not name:
        raise ValueError("--repo must be <owner>/<name>")
    return owner, name


def _run_gh_json(args: list[str], *, stage: str) -> Any:
    cp = run_command(args)
    if cp.returncode != 0:
        raise TransactionError(
            stage=stage,
            message=f"{stage} failed",
            command=" ".join(args),
            output=(cp.stderr or cp.stdout).strip()
        )

    text = (cp.stdout or "").strip()
    if not text:
        raise TransactionError(stage=stage, message=f"{stage} returned empty output", command=" ".join(args))

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransactionError(
            stage=stage,
            message=f"{stage} returned non-json output",
            command=" ".join(args),
            output=str(exc),
        ) from exc


def _run_gh_text(args: list[str], *, stage: str) -> str:
    cp = run_command(args)
    if cp.returncode != 0:
        raise TransactionError(
            stage=stage,
            message=f"{stage} failed",
            command=" ".join(args),
            output=(cp.stderr or cp.stdout).strip()
        )
    return (cp.stdout or "").strip()


# Saturation guard: if result count >= this limit, we cannot guarantee complete exact-match coverage.
# Use `in:title "<escaped title>"` qualifier to scope search to title field; client-side exact match
# is still required because GitHub search tokenizes rather than literal-matches.
#
# _DEDUPE_SEARCH_LIMIT rationale:
# (a) 閾値 200 の根拠: GitHub issue search は tokenize ベースのため完全一致保証がなく、
#     in:title qualifier で絞り込んでも同リポジトリで 200 件超の同語 title が存在する場合は
#     誤判定リスクが無視できない。200 件未満であれば client-side exact-match で完全性を保証できる
#     実運用上の上限として設定している（issue 作成頻度と運用規模から導出）。
# (b) `gh issue list --limit` の実上限との関係: `gh issue list` の CLI レベルに --limit の上限はないが、
#     --search オプション使用時は GitHub Search API 経由になり 1000 件が事実上の上限となる
#     （gh CLI 内部で SearchCapped として処理）。本実装は --search を併用するため Search API 上限の制約を受ける。
#     200 を上限とすることで「件数が多すぎて安全な dedupe 判定不能」な状態を早期検知し、
#     saturation guard として fail-close する（1000 まで許容すると誤ヒット混入リスクが増大する）。
# (c) saturation guard と client-side filter の関係: `gh issue list` が返す件数が _DEDUPE_SEARCH_LIMIT に
#     達した場合、未取得 issue が存在する可能性があるため TransactionError を raise して処理を中断する。
#     件数が上限未満の場合のみ client-side の title 完全一致フィルタが「全件検索済み」として機能する。
_DEDUPE_SEARCH_LIMIT = 200


def _find_open_issues_by_title(repo: str, title: str, gh_bin: str) -> list[int]:
    # Normalize title for search: strip newlines (gh search does not support multi-line queries),
    # escape internal double-quotes.
    normalized = title.replace("\n", " ").replace("\r", " ")
    if normalized.strip() == "":
        raise TransactionError(stage="dedupe-search", message="title is empty or whitespace-only")
    escaped = normalized.replace('"', '\\"')
    search_query = f'in:title "{escaped}"'

    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        search_query,
        "--limit",
        str(_DEDUPE_SEARCH_LIMIT),
        "--json",
        # --json は comma-separated フィールド名を 1 引数で渡す仕様（3 つに分けて渡すと CLI エラー）
        "number,title,url",
    ]
    result = _run_gh_json(args, stage="dedupe-search")
    # Saturation guard: if we hit the limit, we cannot guarantee completeness for exact-match.
    if len(result) >= _DEDUPE_SEARCH_LIMIT:
        raise TransactionError(
            stage="dedupe-search",
            message="dedupe search result saturated; cannot guarantee exact-match completeness",
            output=f"result count={len(result)} >= limit={_DEDUPE_SEARCH_LIMIT}; search_query={search_query!r}",
        )
    # Compare normalized titles to handle GitHub-side newline normalization
    target = normalized.strip()
    issue_numbers: list[int] = []
    for item in result:
        item_title = str(item.get("title", "")).replace("\n", " ").replace("\r", " ").strip()
        if item_title == target:
            issue_numbers.append(int(item["number"]))
    return issue_numbers


# Retry delays for parent readback (sub-issue-readback stage).
# Absorbs GitHub Sub-issues API eventual-consistency lag after POST success.
# Total worst-case wall time = sum(_PARENT_READBACK_RETRY_DELAYS) ≤ 2 seconds.
_PARENT_READBACK_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)

# Retry delays for label readback (label-readback stage).
# Absorbs GitHub label application eventual-consistency lag after POST success.
# Total worst-case wall time = sum(_LABEL_READBACK_RETRY_DELAYS) ≤ 2 seconds.
_LABEL_READBACK_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0)

# Standard labels auto-assigned when issue_kind is "implementation".
# NOTE: state/queued is intentionally NOT included here.
# state/queued is a deprecated label for AI readiness signaling;
# AI착手 eligibility is determined by blocker/dependency close state, not by this label.
_IMPLEMENTATION_STANDARD_LABELS: tuple[str, ...] = (
    "phase/implementation",
    "agent/implementer",
    "enhancement",
)

# Default polling parameters for post-create race detection.
# Sized to cover GitHub search index propagation delay (typically 5-30 seconds).
# Total worst-case wall time = sum(delays) ≈ 32 seconds.
# Override via run_transaction(sleep_fn=...) or _poll_for_created_issue kwargs.
_DEFAULT_MAX_ATTEMPTS: int = 5
_DEFAULT_RACE_DETECTION_DELAYS: tuple[float, ...] = (0.0, 2.0, 5.0, 10.0, 15.0)


def _poll_for_created_issue(
    repo: str,
    title: str,
    expected_issue_number: int,
    gh_bin: str,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    delays: tuple[float, ...] = _DEFAULT_RACE_DETECTION_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
    skip_internal_title_dedupe: bool = False,
) -> tuple[Literal["confirmed", "race", "inconclusive"], list[int]]:
    """Poll for the newly created issue, absorbing GitHub search index propagation delay.

    Returns:
        ("confirmed", [expected_issue_number]) if only our issue is visible.
        ("race", matching) if another issue with the same title is detected.
        ("inconclusive", last_matching) if we could not confirm after max_attempts.

    Raises:
        TransactionError: if the underlying dedupe-search raises (e.g., saturation guard).

    ``skip_internal_title_dedupe`` (Issue #2602 P1-1): when the caller opted into
    ``run_transaction(skip_internal_title_dedupe=True)``, an outer dedupe_key search has
    already confirmed "create confirmed" (AC2(g)), and a different-key/same-title OPEN
    issue is an explicitly ALLOWED case (AC2(b)/(g)) -- it must never be misdetected as a
    post-create race collision. When True, this title-only search is therefore used ONLY
    to confirm the new issue itself became visible; other same-title matches are ignored
    entirely (never downgraded to "race"). Default False preserves the exact prior
    behaviour for every existing caller.
    """
    last_matching: list[int] = []
    for attempt in range(max_attempts):
        delay = delays[attempt] if attempt < len(delays) else delays[-1]
        if delay > 0.0:
            sleep_fn(delay)

        # May raise TransactionError(stage="dedupe-search") for saturation — let it propagate.
        matching = _find_open_issues_by_title(repo, title, gh_bin)
        last_matching = matching

        if skip_internal_title_dedupe:
            # Title-only matching cannot be used as a collision signal here: the outer
            # dedupe_key search is the authoritative identity check, and this script's own
            # internal title-only dedupe was explicitly bypassed for this create. Only
            # confirm our own issue is visible; never flag "race" from other title matches.
            if expected_issue_number in matching:
                return ("confirmed", matching)
            # Not yet visible: search index hasn't propagated yet — retry.
            continue

        if len(matching) == 1 and matching[0] == expected_issue_number:
            # Only our issue is visible: confirmed.
            return ("confirmed", matching)

        if len(matching) > 1 and expected_issue_number in matching:
            # Our issue exists but there are others with the same title: race.
            return ("race", matching)

        if len(matching) >= 1 and expected_issue_number not in matching:
            # Other issues visible but ours is not: race (we may be the duplicate).
            return ("race", matching)

        # matching is empty: search index hasn't propagated yet — retry.

    return ("inconclusive", last_matching)


def _dedupe_search(repo: str, title: str, gh_bin: str) -> int | None:
    issue_numbers = _find_open_issues_by_title(repo, title, gh_bin)
    return issue_numbers[0] if issue_numbers else None


def _issue_create(repo: str, title: str, body: str, body_file: str, gh_bin: str) -> str:
    # AC6 (#1946): the official `gh issue create` CLI exposes its own high-level
    # `--blocked-by`/`--blocking` flags (installed `gh` capability confirmed as of this
    # Issue). This transaction helper deliberately does NOT forward this script's
    # `--blocked-by`/`--dependency`/`--blocking` args to `gh issue create` here.
    # Rationale: `gh issue create`'s native dependency flags are resolved via a
    # deferred/parallel API call after the create mutation returns, with no readback or
    # receipt returned to the caller -- a partial failure there is silent (receipt-loss
    # risk) and would bypass this script's fail-closed readback/audit-comment/recovery-hint
    # machinery entirely. Dependencies are therefore always registered as separate,
    # readback-verified `addBlockedBy` GraphQL mutations (see _issue_register_dependency /
    # _issue_register_blocking / _mutate_add_blocked_by below), never via `_issue_create`.
    # The official CLI's own argument-swap implementation (issueId/blockingIssueId flip
    # between `--blocked-by` and `--blocking`) is used only as the reference implementation
    # for which direction maps to which GraphQL input field; see
    # `_mutate_add_blocked_by`'s docstring and the hard-successor guidance in SKILL.md.
    create_args: list[str] = [
        gh_bin,
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
    ]

    if body_file:
        path = Path(body_file)
        if not path.is_file():
            raise TransactionError(stage="issue-create", message="body-file not found", output=str(path))
        create_args.extend(["--body-file", str(path)])
    else:
        create_args.extend(["--body", body or ""])  # create-issue text body is always explicit

    issue_url = _run_gh_text(create_args, stage="issue-create")
    if not issue_url:
        raise TransactionError(stage="issue-create", message="issue-create returned empty URL")
    return issue_url


def _issue_number_from_url(issue_url: str) -> int:
    match = re.search(r"/issues/(\d+)(?:/.*)?$", issue_url.strip())
    if not match:
        raise TransactionError(stage="issue-create", message="failed to parse issue number", output=issue_url)
    return int(match.group(1))


# Retry delays for the created-issue body readback (body-readback stage).
# Absorbs GitHub read-after-write lag after `gh issue create` returns; bounded, no open-ended wait.
# Total worst-case wall time = sum(_BODY_READBACK_RETRY_DELAYS) <= 2 seconds.
_BODY_READBACK_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)


def _normalize_readback_title(text: str) -> str:
    return text.replace("\n", " ").replace("\r", " ").strip()


def _normalize_readback_body(text: str) -> str:
    """Line-ending and trailing-whitespace normalisation only; content is never rewritten."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _readback_created_issue(
    repo: str,
    issue_number: int,
    issue_url: str,
    title: str,
    expected_body: str,
    gh_bin: str,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Fresh read-only GET of the just-created issue; raise ``body-readback`` unless it matches.

    ``expected_body`` is the body text snapshotted once BEFORE the create (never re-read from
    ``body_file`` here), so the comparison baseline cannot move between create and readback.
    number / title / body / html_url must all be present and equal; a missing field, a non-JSON
    answer, a GET failure or a timeout is a failure, never a skip. Only a bounded retry absorbs
    read-after-write lag. No GitHub mutation is performed.
    """
    expected_title = _normalize_readback_title(title)
    expected_norm = _normalize_readback_body(expected_body)
    attempts = len(_BODY_READBACK_RETRY_DELAYS) + 1
    last_reason = "no attempt"
    for attempt in range(attempts):
        if attempt > 0:
            sleep_fn(_BODY_READBACK_RETRY_DELAYS[attempt - 1])
        try:
            data = _run_gh_json(
                [
                    gh_bin,
                    "api",
                    "--method",
                    "GET",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2022-11-28",
                    f"/repos/{repo}/issues/{issue_number}",
                ],
                stage="body-readback",
            )
        except TransactionError as exc:
            last_reason = f"{exc.message}: {exc.output}"
            continue
        if not isinstance(data, dict) or not all(k in data for k in ("number", "title", "body", "html_url")):
            last_reason = "readback is missing number/title/body/html_url"
            continue
        if data["number"] != issue_number or str(data["html_url"]).strip() != issue_url.strip():
            last_reason = f"readback identity mismatch: number={data['number']!r} url={data['html_url']!r}"
            continue
        if not isinstance(data["title"], str) or _normalize_readback_title(data["title"]) != expected_title:
            last_reason = "remote title does not match the requested title"
            continue
        remote_body = data["body"]
        if remote_body is None:
            remote_body = ""
        if not isinstance(remote_body, str) or _normalize_readback_body(remote_body) != expected_norm:
            last_reason = (
                f"remote body does not match the requested body "
                f"(remote_len={len(remote_body) if isinstance(remote_body, str) else 'n/a'}, "
                f"expected_len={len(expected_norm)})"
            )
            continue
        return
    raise TransactionError(
        stage="body-readback",
        message=f"created issue #{issue_number} could not be verified against the requested title/body "
        f"after {attempts} attempts",
        output=last_reason,
    )


def _issue_apply_labels(repo: str, issue_number: int, labels: list[str], gh_bin: str) -> None:
    if not labels:
        return
    args = [
        gh_bin,
        "issue",
        "edit",
        "--repo",
        repo,
        str(issue_number),
        "--add-label",
        ",".join(labels),
    ]
    _run_gh_text(args, stage="label-apply")


def _issue_graphql_ids(repo: str, issue_number: int, gh_bin: str) -> tuple[str, int]:
    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){id databaseId}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="issue-ids")
    try:
        issue = payload["data"]["repository"]["issue"]
        node_id = issue["id"]
        database_id = int(issue["databaseId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransactionError(stage="issue-ids", message="invalid issue id payload", output=str(payload)) from exc
    if not node_id or not database_id:
        raise TransactionError(stage="issue-ids", message="missing node/database id", output=str(payload))
    return str(node_id), int(database_id)


# ---------------------------------------------------------------------------
# Parent-none detection helpers (Blocker 1)
# ---------------------------------------------------------------------------

# Regex-based "no parent" detection: matches bare keywords and those with
# parenthetical annotations like "none（単独改善）" or "none (standalone)".
_PARENT_NONE_RE = re.compile(
    r"^(?:none|null|n/a|なし|単独改善|0)(?:\s*[（(][^）)]*[）)])?$",
    re.IGNORECASE,
)


def _is_explicit_none(raw: str) -> bool:
    """Return True if raw (stripped) represents an explicit 'no parent' declaration."""
    return bool(_PARENT_NONE_RE.match(raw.strip()))


def _collect_parent_candidates(body: str) -> list[ParentCandidate]:
    """Collect all parent-issue references from body as ParentCandidate objects.

    Recognises the following patterns (case-insensitive for keywords):
      - machine_contract: parent_issue: "#N" / parent_issue: "N" / parent_issue: N
      - parent_heading: ## Parent Issue\\n\\n#N or ## Parent Issue\\n\\nN
      - shorthand: parent: #N / parent: N

    ``Depends on #N`` is explicitly NOT treated as a parent.
    """
    # Normalise line endings
    body = body.replace("\r\n", "\n").replace("\r", "\n")

    candidates: list[ParentCandidate] = []

    def _parse_raw_parent(raw_full: str, source: str) -> ParentCandidate:
        """Parse a raw parent value string into a ParentCandidate.

        Normalisation steps:
          1. strip surrounding whitespace
          2. strip surrounding quotes (single or double)
          3. strip surrounding whitespace again

        Numeric resolution:
          - Bare `#N` or quoted `"#N"` -> strip `#` prefix and parse as integer.
          - Bare `N` or quoted `"N"` -> parse as integer.
          - Values in _PARENT_NONE (case-insensitive) -> explicit none (value=None).
          - Otherwise -> malformed sentinel (value=-1).

        Note on YAML comment ambiguity:
          `#133` unquoted could be interpreted as a YAML comment, but in body
          Markdown context (not strict YAML) we treat `#N` as a parent reference.
        """
        raw = raw_full.strip().strip("\"'").strip()
        if _is_explicit_none(raw):
            return ParentCandidate(source=source, raw=raw, value=None)
        # Try to parse as number (with optional leading #)
        m_num = re.match(r'^#?(\d+)$', raw.strip())
        if m_num:
            return ParentCandidate(source=source, raw=raw, value=int(m_num.group(1)))
        return ParentCandidate(source=source, raw=raw, value=-1)  # sentinel for malformed

    # Pattern 1 (machine_contract): parent_issue: "#N" / parent_issue: "N/A" / parent_issue: N
    # Regex captures the full value to end-of-line (after stripping YAML-comment portion for
    # unquoted values). For quoted values, we capture up to the closing quote.
    for m in re.finditer(
        r'(?im)^[ \t]*parent_issue\s*:\s*(.+?)\s*$',
        body,
    ):
        raw_full = m.group(1).strip()
        # Strip inline YAML comment for unquoted values (e.g. "42 # comment" -> "42")
        # But do NOT strip for quoted values (e.g. '"#133"' stays as-is).
        if raw_full and raw_full[0] not in ('"', "'"):
            # Remove YAML comment: everything from ` #` onward that is NOT part of a number
            # Preserve bare `#N` patterns (treated as issue references).
            comment_match = re.match(r'^(#?\d+|[^\s#].*?)\s*(#.*)?$', raw_full)
            if comment_match:
                raw_full = comment_match.group(1)
        candidates.append(_parse_raw_parent(raw_full, "machine_contract"))

    # Pattern 2 (parent_heading): ## Parent Issue\n\n#N  or  ## Parent Issue\n\nN
    for m in re.finditer(
        r'(?im)^##\s*Parent Issue\s*\n(?:\s*\n)*\s*(.+?)[ \t]*$',
        body,
    ):
        raw_full = m.group(1).strip()
        candidates.append(_parse_raw_parent(raw_full, "parent_heading"))

    # Pattern 3 (shorthand): parent: #N / parent: N  (but NOT "Depends on" lines)
    for line in body.splitlines():
        if re.search(r'(?i)depends\s+on', line):
            continue
        m = re.match(r'(?i)^[ \t]*parent\s*:\s*(.+?)\s*$', line)
        if m:
            raw_full = m.group(1).strip()
            candidates.append(_parse_raw_parent(raw_full, "shorthand"))

    return candidates


def _extract_parent_resolution_from_body(body: str) -> ParentResolution:
    """Extract parent issue resolution (tri-state) from issue body text.

    Uses candidate collection (_collect_parent_candidates) and applies conflict
    detection rules:
      - 0 candidates -> ParentResolution(state="absent")
      - 1 candidate, valid number -> ParentResolution(state="number", value=N)
      - 1 candidate, explicit none/null/N/A -> ParentResolution(state="explicit_none")
      - 1 candidate, malformed (non-integer, non-none) -> TransactionError(stage="parent-body-parse")
      - Multiple candidates, all same number -> PASS (state="number", value=N)
      - explicit none + number mixed -> TransactionError(stage="parent-body-conflict")
      - multiple numbers that differ -> TransactionError(stage="parent-body-conflict")
    """
    candidates = _collect_parent_candidates(body)

    if not candidates:
        return ParentResolution(state="absent")

    if len(candidates) == 1:
        c = candidates[0]
        if c.value == -1:
            raise TransactionError(
                stage="parent-body-parse",
                message=f"parent expression in body is malformed (not a valid number): {c.raw!r}",
                output=f"source={c.source} raw={c.raw!r}",
            )
        if c.value is None:
            return ParentResolution(state="explicit_none")
        return ParentResolution(state="number", value=c.value)

    # Multiple candidates: check for conflicts.
    # Partition into "explicit none" and "numeric" groups (malformed = -1 is also a conflict).
    has_malformed = any(c.value == -1 for c in candidates)
    if has_malformed:
        raise TransactionError(
            stage="parent-body-parse",
            message=(
                "parent expression in body is malformed (not a valid number): "
                f"{[c.raw for c in candidates if c.value == -1]}"
            ),
            output=str([(c.source, c.raw) for c in candidates]),
        )

    none_candidates = [c for c in candidates if c.value is None]
    number_candidates = [c for c in candidates if c.value is not None]

    if none_candidates and number_candidates:
        raise TransactionError(
            stage="parent-body-conflict",
            message=(
                "body contains both explicit-none parent and numeric parent references; "
                f"none-sources={[c.source for c in none_candidates]} "
                f"number-sources={[c.source for c in number_candidates]} "
                f"number-values={[c.value for c in number_candidates]}"
            ),
            output=str([(c.source, c.raw, c.value) for c in candidates]),
        )

    if number_candidates:
        unique_numbers = {c.value for c in number_candidates}
        if len(unique_numbers) > 1:
            raise TransactionError(
                stage="parent-body-conflict",
                message=(
                    f"body contains conflicting parent numbers: {sorted(unique_numbers)}; "
                    f"sources={[(c.source, c.value) for c in number_candidates]}"
                ),
                output=str([(c.source, c.raw, c.value) for c in candidates]),
            )
        return ParentResolution(state="number", value=number_candidates[0].value)

    # All candidates are explicit none -> no parent.
    return ParentResolution(state="explicit_none")


def _extract_parent_issue_number_from_body(body: str) -> int | None:
    """Backward-compatible wrapper around _extract_parent_resolution_from_body.

    Returns:
      - None for absent or explicit_none states
      - int for number state

    NOTE: This wrapper loses the tri-state distinction. Use
    _extract_parent_resolution_from_body() directly when you need to distinguish
    "absent" from "explicit_none" (e.g. for CLI arg conflict detection).
    """
    resolution = _extract_parent_resolution_from_body(body)
    if resolution.state == "number":
        return resolution.value
    return None


def _extract_field_from_body(body: str, field_name: str) -> str | None:
    """Extract a machine-readable contract field value from issue body.

    Looks for YAML-style ``field_name: value`` lines (optionally quoted).
    Returns the stripped value string, or None if not found or unparseable.
    This is a best-effort helper; None means "skip the gate".
    """
    pattern = re.compile(
        r'(?im)^[ \t]*' + re.escape(field_name) + r'\s*:\s*(.+?)\s*$',
    )
    m = pattern.search(body)
    if not m:
        return None
    value = m.group(1).strip().strip("\"'").strip()
    return value if value else None


def _resolve_parent_issue_number(parent_arg: int, body_resolution: ParentResolution) -> int:
    """Resolve the effective parent issue number from CLI arg and body-derived resolution.

    Rules (tri-state):
      body absent + parent_arg > 0  -> parent_arg (absent is neutral; CLI wins)
      body absent + parent_arg == 0 -> 0 (no parent)
      body explicit_none + parent_arg == 0  -> 0 (no parent; explicit none honoured)
      body explicit_none + parent_arg > 0   -> TransactionError(stage="parent-arg-body-mismatch")
      body number N + parent_arg == 0  -> N (body wins)
      body number N + parent_arg == N  -> N (agreement)
      body number N + parent_arg != N (and != 0) -> TransactionError(stage="parent-arg-body-mismatch")
    """
    if body_resolution.state == "explicit_none":
        if parent_arg > 0:
            raise TransactionError(
                stage="parent-arg-body-mismatch",
                message=(
                    f"--parent-issue {parent_arg} contradicts body parent_issue: none; "
                    "failing closed. Remove --parent-issue or update body to declare a parent."
                ),
                output=f"parent_arg={parent_arg} body_resolution=explicit_none",
            )
        return 0

    if body_resolution.state == "number":
        body_number = body_resolution.value
        if parent_arg > 0 and parent_arg != body_number:
            raise TransactionError(
                stage="parent-arg-body-mismatch",
                message=(
                    f"--parent-issue {parent_arg} contradicts body parent #{body_number}; "
                    "failing closed. Fix the body or omit --parent-issue."
                ),
                output=f"parent_arg={parent_arg} body_parent={body_number}",
            )
        if parent_arg > 0:
            return parent_arg
        return body_number  # type: ignore[return-value]

    # state == "absent": body has no parent declaration; CLI arg takes precedence
    if parent_arg > 0:
        return parent_arg
    return 0


def _issue_register_sub_issue_idempotent(
    repo: str,
    parent_issue_number: int,
    child_database_id: int,
    child_issue_number: int,
    gh_bin: str,
) -> str:
    """Register child as sub-issue of parent, treating 422 as idempotent when already registered.

    Returns:
        "registered"         — POST succeeded (HTTP 201/200).
        "already_registered" — POST returned HTTP 422 and read-back confirmed same parent.

    Raises:
        TransactionError(stage="sub-issue-register") — POST failed for reasons
            other than 422, or 422 but read-back showed a *different* parent
            (or no parent at all).  This is treated as partial_failure.
    """
    args = [
        gh_bin,
        "api",
        "--method",
        "POST",
        "--include",  # include HTTP response headers in output for status parsing
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"/repos/{repo}/issues/{parent_issue_number}/sub_issues",
        "-F",
        f"sub_issue_id={child_database_id}",
    ]
    cp = run_command(args)

    # Collect combined output (stdout contains headers + body when --include is used)
    stdout_out = (cp.stdout or "").strip()
    stderr_out = (cp.stderr or "").strip()
    combined_output = stdout_out + ("\n" + stderr_out if stderr_out else "")

    if cp.returncode == 0:
        return "registered"

    # Determine if the failure is an HTTP 422 using the parsed status line.
    http_status = _extract_http_status(combined_output)
    is_422 = http_status == 422

    if not is_422:
        raise TransactionError(
            stage="sub-issue-register",
            message="sub-issue POST failed with non-422 error",
            command=" ".join(args),
            output=combined_output,
        )

    # HTTP 422: read-back to check if already registered with the same parent
    readback_args = [
        gh_bin,
        "api",
        "--method",
        "GET",
        "--include",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"/repos/{repo}/issues/{child_issue_number}/parent",
    ]
    rb_cp = run_command(readback_args)
    rb_stdout = (rb_cp.stdout or "").strip()
    rb_stderr = (rb_cp.stderr or "").strip()
    rb_combined = rb_stdout + ("\n" + rb_stderr if rb_stderr else "")
    rb_http_status = _extract_http_status(rb_combined)

    if rb_cp.returncode == 0 and rb_http_status == 200:
        # Strip HTTP headers from body for JSON parsing (--include prepends headers before blank line)
        rb_body = rb_stdout
        if "\r\n\r\n" in rb_body:
            rb_body = rb_body.split("\r\n\r\n", 1)[1]
        elif "\n\n" in rb_body:
            rb_body = rb_body.split("\n\n", 1)[1]
        try:
            data = json.loads(rb_body.strip() or "{}")
            if int(data.get("number", -1)) == int(parent_issue_number):
                return "already_registered"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    raise TransactionError(
        stage="sub-issue-register",
        message=(
            f"sub-issue POST returned HTTP 422 but read-back did not confirm parent #{parent_issue_number} "
            f"for child #{child_issue_number}; treating as partial_failure."
        ),
        command=" ".join(args),
        output=combined_output,
    )


def _mutate_add_blocked_by(
    repo: str,
    gh_bin: str,
    *,
    blocked_issue_node_id: str,
    blocking_issue_node_id: str,
    stage: str,
) -> None:
    """Role-fixed GraphQL ``addBlockedBy`` primitive (see #1946 Notes for Reviewer).

    ``blocked_issue_node_id``/``blocking_issue_node_id`` are keyword-only and named after
    their GitHub API role (``issueId``/``blockingIssueId``) so the caller cannot silently
    swap direction. A generic ``direction="reverse"``-style flag is intentionally NOT
    provided here: that shape is what caused the #1943/#1842 reversal bug this Issue fixes.
    """
    query = "mutation($input:AddBlockedByInput!){addBlockedBy(input:$input){clientMutationId}}"
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"input[issueId]={blocked_issue_node_id}",
        "-F",
        f"input[blockingIssueId]={blocking_issue_node_id}",
    ]
    _run_gh_text(args, stage=stage)


def _issue_register_dependency(repo: str, child_node_id: str, dependency_node_id: str, gh_bin: str) -> None:
    """``--blocked-by``/``--dependency`` direction: child_node_id (the new/target issue) is
    blocked by dependency_node_id."""
    _mutate_add_blocked_by(
        repo,
        gh_bin,
        blocked_issue_node_id=child_node_id,
        blocking_issue_node_id=dependency_node_id,
        stage="dependency-register",
    )


def _issue_register_blocking(repo: str, child_node_id: str, target_node_id: str, gh_bin: str) -> None:
    """``--blocking`` direction: child_node_id (the new/target issue) BLOCKS target_node_id
    (i.e. target_node_id becomes blocked by child_node_id -- the hard-predecessor direction).
    """
    _mutate_add_blocked_by(
        repo,
        gh_bin,
        blocked_issue_node_id=target_node_id,
        blocking_issue_node_id=child_node_id,
        stage="blocking-register",
    )


@dataclass(frozen=True)
class LabelReadbackResult:
    ok: bool
    expected_labels: list[str]
    actual_labels: list[str]
    attempts: int
    retry_delays: list[float]
    last_error: str | None = None
    # missing_expected_labels | invalid_payload | command_error | rate_limited
    error_kind: str = "missing_expected_labels"


def _readback_labels_once(repo: str, issue_number: int, labels: list[str], gh_bin: str) -> bool:
    """Single attempt to read back labels from GitHub GraphQL API.

    Returns True if all expected labels are confirmed, False otherwise.
    Raises TransactionError for payload errors.
    """
    if not labels:
        return True
    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){labels(first:100){nodes{name}}}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="label-readback")
    try:
        labels_payload = (
            payload["data"]["repository"]["issue"]
            .get("labels", {})
            .get("nodes", [])
        )
        current_labels = {str(item.get("name")) for item in labels_payload if item.get("name") is not None}
    except (KeyError, TypeError) as exc:
        raise TransactionError(
            stage="label-readback",
            message="invalid label read-back payload",
            output=str(payload)
        ) from exc
    return set(labels).issubset(current_labels)


def _readback_labels_with_result(
    repo: str,
    issue_number: int,
    labels: list[str],
    gh_bin: str,
    *,
    delays: tuple[float, ...] = _LABEL_READBACK_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> LabelReadbackResult:
    """Read back labels with retry, returning a structured LabelReadbackResult.

    Fetches actual labels from GitHub GraphQL API so actual_labels is never []
    unless the issue genuinely has no labels.
    """
    if not labels:
        return LabelReadbackResult(
            ok=True,
            expected_labels=list(labels),
            actual_labels=[],
            attempts=0,
            retry_delays=[],
        )

    def _fetch_actual_labels() -> list[str]:
        """Fetch the current labels from GitHub GraphQL API."""
        owner, name = _github_owner_repo(repo)
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){"
            "issue(number:$number){labels(first:100){nodes{name}}}"
            "}}"
        )
        args = [
            gh_bin,
            "api",
            "graphql",
            "-f",
            "query=\n" + query,
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={issue_number}",
        ]
        try:
            payload = _run_gh_json(args, stage="label-readback")
            labels_payload = (
                payload["data"]["repository"]["issue"]
                .get("labels", {})
                .get("nodes", [])
            )
            return [str(item.get("name")) for item in labels_payload if item.get("name") is not None]
        except (TransactionError, KeyError, TypeError):
            return []

    attempts = 0
    last_actual: list[str] = []
    last_error: str | None = None
    error_kind: str = "missing_expected_labels"

    # Initial attempt
    attempts += 1
    try:
        ok = _readback_labels_once(repo, issue_number, labels, gh_bin)
        if ok:
            last_actual = _fetch_actual_labels()
            return LabelReadbackResult(
                ok=True,
                expected_labels=list(labels),
                actual_labels=last_actual,
                attempts=attempts,
                retry_delays=[],
            )
        last_actual = _fetch_actual_labels()
    except TransactionError as exc:
        # Blocker A: catch TransactionError from _readback_labels_once; do NOT propagate.
        # Attempt to fetch actual_labels for diagnostics (Blocker E).
        last_error = exc.message
        error_kind = "command_error"
        fetched = _fetch_actual_labels()
        if fetched:
            last_actual = fetched

    used_delays: list[float] = []
    for delay in delays:
        sleep_fn(delay)
        used_delays.append(delay)
        attempts += 1
        try:
            ok = _readback_labels_once(repo, issue_number, labels, gh_bin)
            if ok:
                last_actual = _fetch_actual_labels()
                return LabelReadbackResult(
                    ok=True,
                    expected_labels=list(labels),
                    actual_labels=last_actual,
                    attempts=attempts,
                    retry_delays=used_delays,
                )
            fetched = _fetch_actual_labels()
            if fetched:
                last_actual = fetched
        except TransactionError as exc:
            # Blocker A: catch TransactionError from _readback_labels_once; do NOT propagate.
            last_error = exc.message
            error_kind = "command_error"
            fetched = _fetch_actual_labels()
            if fetched:
                last_actual = fetched

    return LabelReadbackResult(
        ok=False,
        expected_labels=list(labels),
        actual_labels=last_actual,
        attempts=attempts,
        retry_delays=used_delays,
        last_error=last_error,
        error_kind=error_kind,
    )


def _readback_labels(
    repo: str,
    issue_number: int,
    labels: list[str],
    gh_bin: str,
    *,
    delays: tuple[float, ...] = _LABEL_READBACK_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Read back labels with retry to absorb GitHub eventual-consistency lag.

    Returns True if all expected labels are confirmed within the retry budget.
    """
    if not labels:
        return True
    if _readback_labels_once(repo, issue_number, labels, gh_bin):
        return True
    for delay in delays:
        sleep_fn(delay)
        if _readback_labels_once(repo, issue_number, labels, gh_bin):
            return True
    return False


def _readback_parent_issue(repo: str, issue_number: int, parent_issue_number: int, gh_bin: str) -> bool:
    # GET repos/{owner}/{repo}/issues/{issue_number}/parent.
    # `gh api` switches the default method to POST as soon as any -f/-F request
    # parameter is supplied, so the Accept value must be passed as a header (-H),
    # not as a request parameter, and the method must be pinned to GET explicitly.
    args = [
        gh_bin,
        "api",
        f"repos/{repo}/issues/{issue_number}/parent",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    cp = run_command(args)
    if cp.returncode != 0:
        return False
    try:
        data = json.loads((cp.stdout or "").strip() or "{}")
        return int(data.get("number", -1)) == int(parent_issue_number)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def _readback_parent_issue_with_retry(
    repo: str,
    issue_number: int,
    parent_issue_number: int,
    gh_bin: str,
    *,
    delays: tuple[float, ...] = _PARENT_READBACK_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Read back parent issue, retrying on transient failure with bounded delays.

    Absorbs GitHub Sub-issues API eventual-consistency lag after POST success.
    Returns True if parent is confirmed within the retry budget, False otherwise.
    Total wait = sum(delays) which must remain ≤ 2 seconds per AC1 contract.
    """
    if _readback_parent_issue(repo, issue_number, parent_issue_number, gh_bin):
        return True
    for delay in delays:
        sleep_fn(delay)
        if _readback_parent_issue(repo, issue_number, parent_issue_number, gh_bin):
            return True
    return False


def _readback_dependencies(repo: str, issue_number: int, dependency_issue_numbers: list[int], gh_bin: str) -> bool:
    """Readback the ``--blocked-by``/``--dependency`` direction (AC4).

    Requires strict set equality (not merely a subset match) between the requested
    dependency numbers and the issue's ``blockedBy`` connection, and additionally
    requires ``pageInfo.hasNextPage == False`` and ``totalCount == len(expected)`` so a
    truncated/paginated read cannot be silently treated as a match.
    """
    if not dependency_issue_numbers:
        return True

    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){blockedBy(first:100){totalCount pageInfo{hasNextPage} nodes{number}}}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="dependency-readback")
    try:
        issue_payload = payload["data"]["repository"]["issue"]
        if issue_payload is None:
            # GraphQL responded successfully but the issue is null (deleted/inaccessible):
            # a structured False, not a traceback (#1946 Owner P1-3).
            return False
        conn = issue_payload.get("blockedBy", {}) or {}
        nodes = conn.get("nodes", []) or []
        readback_numbers = {int(item.get("number")) for item in nodes if item.get("number") is not None}
        total_count = conn.get("totalCount")
        has_next_page = conn.get("pageInfo", {}).get("hasNextPage")
    except (KeyError, TypeError, AttributeError):
        return False
    expected = set(dependency_issue_numbers)
    if readback_numbers != expected:
        return False
    if has_next_page is not False:
        return False
    if total_count != len(expected):
        return False
    return True


# max_pages guard: caps full-connection pagination at 50 pages * 100 nodes/page = 5000
# relationships. This bounds worst-case GraphQL round trips for _paginated_relationship_numbers
# while remaining far above any realistic blockedBy/blocking connection size.
_RELATIONSHIP_PAGINATION_MAX_PAGES = 50


def _paginated_relationship_numbers(
    repo: str, target_issue_number: int, connection_field: str, gh_bin: str
) -> tuple[str, set[int]]:
    """Fully paginate a ``blockedBy``/``blocking`` connection for ``target_issue_number``
    (#1946 Owner P1-3: a single ``first:100`` page is not enough once a target issue has
    more than 100 relationships in that direction; this walks every page via cursor).

    Returns ``(status, numbers)`` where ``status`` is one of:
      "ok"              — ``numbers`` contains the complete connection (all pages).
      "issue_not_found" — GraphQL responded successfully but ``issue`` is null
                           (deleted/inaccessible issue); a structured failure, never a
                           traceback.
      "invalid_payload" — the payload shape was unexpected (missing keys, or pagination
                           did not terminate within ``_RELATIONSHIP_PAGINATION_MAX_PAGES``).
    """
    owner, name = _github_owner_repo(repo)
    numbers: set[int] = set()
    after: str | None = None
    # NOTE: connection_field is interpolated via plain string concatenation (not an
    # f-string) on purpose -- mixing f-string {{ }} escaping with plain-string literal
    # braces in the same expression is exactly the kind of off-by-one-brace bug this
    # helper must not have (a prior draft of this function had this bug and produced
    # invalid GraphQL that gh rejected with a syntax error, silently downgrading every
    # --blocking readback to "invalid_payload" -- caught by the #1946 AC8 live canary).
    query = (
        "query($owner:String!,$name:String!,$number:Int!,$after:String){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){"
        + connection_field
        + "(first:100,after:$after){"
        "totalCount pageInfo{hasNextPage endCursor} nodes{number}"
        "}}}}"
    )
    for _ in range(_RELATIONSHIP_PAGINATION_MAX_PAGES):
        args = [
            gh_bin,
            "api",
            "graphql",
            "-f",
            "query=\n" + query,
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={target_issue_number}",
        ]
        if after:
            args += ["-F", f"after={after}"]
        try:
            payload = _run_gh_json(args, stage="relationship-readback")
            repository = payload["data"]["repository"]
        except (TransactionError, KeyError, TypeError):
            return "invalid_payload", set()
        if not isinstance(repository, dict):
            return "invalid_payload", set()
        issue_payload = repository.get("issue")
        if issue_payload is None:
            # GraphQL responded successfully but the issue is null: structured failure,
            # never an AttributeError traceback (#1946 Owner P1-3).
            return "issue_not_found", set()
        conn = issue_payload.get(connection_field) or {}
        nodes = conn.get("nodes", []) or []
        for item in nodes:
            item_number = item.get("number")
            if item_number is not None:
                numbers.add(int(item_number))
        page_info = conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            after = page_info.get("endCursor")
            if not after:
                return "invalid_payload", numbers
            continue
        return "ok", numbers
    return "invalid_payload", numbers


def _target_blockedby_contains(repo: str, target_issue_number: int, expected_issue_number: int, gh_bin: str) -> bool:
    """Return True if ``target_issue_number``'s ``blockedBy`` connection contains
    ``expected_issue_number`` (used as the target-side cross-check for the ``--blocking``
    direction: AC4 requires confirming each blocking target's blockedBy sees the new
    issue, not just that the new issue's own ``blocking`` connection reports it).

    Uses full pagination (#1946 Owner P1-3) so a target issue with more than 100
    existing blockers does not produce a false negative when the expected issue is on a
    later page, and treats a null ``issue`` payload as a structured non-match rather than
    an uncaught ``AttributeError``.
    """
    status, numbers = _paginated_relationship_numbers(repo, target_issue_number, "blockedBy", gh_bin)
    if status != "ok":
        return False
    return expected_issue_number in numbers


def _current_relationship_numbers(
    repo: str,
    issue_number: int,
    direction: Literal["blocked_by", "blocking"],
    gh_bin: str,
) -> tuple[str, set[int]]:
    """Return the current (fully paginated) relationship numbers for ``issue_number`` in
    the given direction. Thin, direction-aware wrapper around
    ``_paginated_relationship_numbers`` used by ``_apply_relationship_direction`` to
    compute the mutation diff (#1946 Owner P0-1)."""
    connection_field = "blockedBy" if direction == "blocked_by" else "blocking"
    return _paginated_relationship_numbers(repo, issue_number, connection_field, gh_bin)


@dataclass(frozen=True)
class RelationshipApplyResult:
    """Result of a shared relationship apply/readback pass (#1946 Owner P0-1)."""

    verified: bool
    mutated_numbers: list[int]
    already_present_numbers: list[int]
    completed_steps: list[str]
    failed_readback: dict | None = None


def _apply_relationship_direction(
    repo: str,
    issue_number: int,
    desired_issue_numbers: list[int],
    direction: Literal["blocked_by", "blocking"],
    gh_bin: str,
    *,
    assume_current_empty: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RelationshipApplyResult:
    """Shared relationship apply/readback helper used by the create, dedupe
    (``_reconcile_issue_links``), and ``reconcile_transaction`` paths alike (#1946 Owner
    P0-1). Computes the diff between ``desired_issue_numbers`` and the issue's CURRENT
    relationship state (full pagination via ``_current_relationship_numbers``), mutates
    ONLY the numbers that are missing (already-present numbers are never re-mutated),
    then re-reads back to confirm the final state strictly matches ``desired_issue_numbers``.

    ``assume_current_empty=True`` skips the current-state query entirely and treats every
    desired number as missing. This is safe (and avoids a redundant GraphQL round trip)
    only for a just-created issue, which by construction cannot already have any
    blockedBy/blocking relationships.
    """
    step_name = "dependency" if direction == "blocked_by" else "blocking"

    if not desired_issue_numbers:
        return RelationshipApplyResult(
            verified=True, mutated_numbers=[], already_present_numbers=[], completed_steps=[]
        )

    desired_set = set(desired_issue_numbers)

    if assume_current_empty:
        current_numbers: set[int] = set()
        missing = sorted(desired_set)
    else:
        status, current_numbers = _current_relationship_numbers(repo, issue_number, direction, gh_bin)
        if status != "ok":
            return RelationshipApplyResult(
                verified=False,
                mutated_numbers=[],
                already_present_numbers=[],
                completed_steps=[],
                failed_readback={
                    "stage": f"{step_name}-readback",
                    "direction": direction,
                    "expected_issue_numbers": sorted(desired_set),
                    "actual_issue_numbers": "actual_state_unavailable",
                    "expected_labels": [],
                    "actual_labels": [],
                    "attempts": 1,
                    "retry_delays": [],
                    "error_kind": "actual_state_unavailable" if status == "issue_not_found" else "invalid_payload",
                    "message": f"{step_name} current-state readback returned {status} for #{issue_number}",
                },
            )
        missing = sorted(desired_set - current_numbers)

    completed_steps: list[str] = []
    if missing:
        child_node_id, _ = _issue_graphql_ids(repo, issue_number, gh_bin)
        for target_number in missing:
            target_node_id, _ = _issue_graphql_ids(repo, target_number, gh_bin)
            if direction == "blocked_by":
                _issue_register_dependency(repo, child_node_id, target_node_id, gh_bin)
            else:
                _issue_register_blocking(repo, child_node_id, target_node_id, gh_bin)
        completed_steps.append(step_name)

    verify_fn = _readback_dependencies if direction == "blocked_by" else _readback_blocking
    verified = verify_fn(repo, issue_number, sorted(desired_set), gh_bin)
    if not verified:
        sleep_fn(2.0)
        verified = verify_fn(repo, issue_number, sorted(desired_set), gh_bin)

    if verified:
        completed_steps.append(f"{step_name}-readback")
        return RelationshipApplyResult(
            verified=True,
            mutated_numbers=missing,
            already_present_numbers=sorted(desired_set & current_numbers),
            completed_steps=completed_steps,
        )

    # Final readback failed: fetch actual numbers for diagnostics (best-effort). Skipped
    # for assume_current_empty (create path) to avoid an extra live query on a failure
    # path that is already about to raise/report; "actual_state_unavailable" is reported
    # instead (#1946 Owner P1-2).
    if assume_current_empty:
        actual_report: Any = "actual_state_unavailable"
    else:
        actual_status, actual_numbers = _current_relationship_numbers(repo, issue_number, direction, gh_bin)
        actual_report = sorted(actual_numbers) if actual_status == "ok" else "actual_state_unavailable"

    return RelationshipApplyResult(
        verified=False,
        mutated_numbers=missing,
        already_present_numbers=sorted(desired_set & current_numbers),
        completed_steps=completed_steps,
        failed_readback={
            "stage": f"{step_name}-readback",
            "direction": direction,
            "expected_issue_numbers": sorted(desired_set),
            "actual_issue_numbers": actual_report,
            "expected_labels": [],
            "actual_labels": [],
            "attempts": 2,
            "retry_delays": [2.0],
            "error_kind": "missing_expected_labels",
            "message": f"{step_name} read-back mismatch after retries",
        },
    )


def _readback_blocking(repo: str, issue_number: int, blocking_issue_numbers: list[int], gh_bin: str) -> bool:
    """Readback the ``--blocking`` direction (AC4).

    Verifies (a) the new issue's own ``blocking`` connection strictly matches
    ``blocking_issue_numbers`` (set equality + pageInfo/totalCount, mirroring
    ``_readback_dependencies``), AND (b) each blocking target issue's ``blockedBy``
    connection contains the new issue (target-side cross-check).
    """
    if not blocking_issue_numbers:
        return True

    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){blocking(first:100){totalCount pageInfo{hasNextPage} nodes{number}}}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="blocking-readback")
    try:
        issue_payload = payload["data"]["repository"]["issue"]
        if issue_payload is None:
            # GraphQL responded successfully but the issue is null (deleted/inaccessible):
            # a structured False, not a traceback (#1946 Owner P1-3).
            return False
        conn = issue_payload.get("blocking", {}) or {}
        nodes = conn.get("nodes", []) or []
        readback_numbers = {int(item.get("number")) for item in nodes if item.get("number") is not None}
        total_count = conn.get("totalCount")
        has_next_page = conn.get("pageInfo", {}).get("hasNextPage")
    except (KeyError, TypeError, AttributeError):
        return False
    expected = set(blocking_issue_numbers)
    if readback_numbers != expected:
        return False
    if has_next_page is not False:
        return False
    if total_count != len(expected):
        return False
    for target_number in blocking_issue_numbers:
        if not _target_blockedby_contains(repo, target_number, issue_number, gh_bin):
            return False
    return True


def _recovery_hint_for_stage(
    failed_stage: str,
    repo: str,
    issue_number: int,
    requested_parent_issue_number: int,
    requested_dependency_issue_numbers: list[int],
    requested_blocking_issue_numbers: list[int] | None = None,
) -> str:
    """Return a stage-specific recovery hint string."""
    owner_repo = repo  # format: owner/repo
    if failed_stage == "sub-issue-readback":
        parent = requested_parent_issue_number
        return (
            f"Recovery hint: sub-issue-readback failed for #{issue_number} under parent #{parent}.\n"
            "  Manual re-register command (idempotent: "
            f"既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり）):\n"
            f"  1. まず readback で関係の有無を確認する:\n"
            f"    gh api repos/{owner_repo}/issues/{issue_number}/parent\n"
            f"  2. 未登録が確認できた場合のみ登録 mutation を実行する:\n"
            f"    gh api repos/{owner_repo}/issues/{parent}/sub_issues --method POST -F sub_issue_id=<child_db_id>\n"
            f"  Lookup child database ID:\n"
            f"    gh api graphql -f query='query{{repository"
            f"(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number})"
            "{{databaseId}}}}}}'\n"
            f"  Verify:\n"
            f"    gh api repos/{owner_repo}/issues/{issue_number}/parent"
        )
    if failed_stage in ("dependency-readback", "dependency-register"):
        deps = (
            ", "
        ).join(f"#{d}" for d in requested_dependency_issue_numbers) if requested_dependency_issue_numbers else "(none)"
        return (
            f"Recovery hint: {failed_stage} failed for #{issue_number} (blockers: {deps}).\n"
            "  Manual re-register command (idempotent: "
            "既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり）):\n"
            f"  1. まず readback で blockedBy 関係の有無を確認する:\n"
            f"    gh api graphql -f query='query{{repository"
            f"(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number})"
            "{{blockedBy(first:10){{nodes{{number}}}}}}}}}}'\n"
            "  2. 未登録が確認できた場合のみ登録 mutation を実行する:\n"
            "    gh api graphql -f query='mutation"
            "($input:AddBlockedByInput!){addBlockedBy(input:$input)"
            "{clientMutationId}}' \\\n"
            "      -F 'input[issueId]=<child_node_id>' -F 'input[blockingIssueId]=<blocker_node_id>'\n"
            "  Lookup node IDs:\n"
            "    gh api graphql -f query='query{{repository"
            "(owner:\"<owner>\",name:\"<repo>\"){{issue(number:<N>)"
            "{{id}}}}}}'\n"
            "  Verify:\n"
            f"    gh api graphql -f query='query{{repository"
            f"(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number})"
            "{{blockedBy(first:10){{nodes{{number}}}}}}}}}}'"
        )
    if failed_stage in ("blocking-readback", "blocking-register"):
        targets = requested_blocking_issue_numbers or []
        targets_text = (", ").join(f"#{d}" for d in targets) if targets else "(none)"
        return (
            f"Recovery hint: {failed_stage} failed for #{issue_number} (blocking targets: {targets_text}).\n"
            "  Manual re-register command (idempotent: "
            "既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり）):\n"
            f"  1. まず readback で blocking 関係の有無を確認する:\n"
            f"    gh api graphql -f query='query{{repository"
            f"(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number})"
            "{{blocking(first:10){{nodes{{number}}}}}}}}}}'\n"
            "  2. 未登録が確認できた場合のみ登録 mutation を実行する（--blocking 方向は issueId/"
            "blockingIssueId の引数が --blocked-by と swap される点に注意。参照実装: "
            "official `gh issue create --blocked-by`/`--blocking` の argument-swap 実装）:\n"
            "    gh api graphql -f query='mutation"
            "($input:AddBlockedByInput!){addBlockedBy(input:$input)"
            "{clientMutationId}}' \\\n"
            "      -F 'input[issueId]=<target_node_id>' -F 'input[blockingIssueId]=<new_issue_node_id>'\n"
            "  Lookup node IDs:\n"
            "    gh api graphql -f query='query{{repository"
            "(owner:\"<owner>\",name:\"<repo>\"){{issue(number:<N>)"
            "{{id}}}}}}'\n"
            "  Verify (target-side blockedBy must contain the new issue):\n"
            f"    gh api graphql -f query='query{{repository"
            f"(owner:\"<owner>\",name:\"<repo>\"){{issue(number:<target>)"
            "{{blockedBy(first:10){{nodes{{number}}}}}}}}}}'"
        )
    if failed_stage in ("label-readback", "dedupe-label-readback"):
        return (
            f"Recovery hint: {failed_stage} failed for #{issue_number}.\n"
            "  Script-mediated re-apply command (idempotent: yes):\n"
            "    uv run python3 .claude/skills/create-issue/scripts/"
            f"create_issue_txn.py reconcile --repo {owner_repo}"
            f" --issue {issue_number} --label <labels>\n"
            "  PROHIBITED: raw direct mutation commands are forbidden.\n"
            "  Recovery MUST go through the reconcile subcommand for audit trail and idempotency.\n"
            "  Verify:\n"
            f"    gh issue view {issue_number} --repo {owner_repo} --json labels"
        )
    if failed_stage in ("dedupe-search", "dedupe-race-detection"):
        return (
            f"Recovery hint: {failed_stage} — automatic recovery is not possible.\n"
            "  Manual action required: inspect open issues with the same title and decide which to close.\n"
            f"    gh issue list --repo {owner_repo} --state open --search '<title>'\n"
            "  idempotent re-run: no (requires manual deduplication first)"
        )
    return (
        f"Recovery hint: stage '{failed_stage}' encountered an unexpected failure.\n"
        "  Generic guidance: verify the issue state manually and re-run the transaction if safe.\n"
        f"    gh issue view {issue_number} --repo {owner_repo} --json number,title,labels,state\n"
        "  idempotent re-run: depends on the specific failure"
    )


def _post_partial_failure_comment(
    repo: str,
    issue_number: int,
    failed_stage: str,
    failure_message: str,
    gh_bin: str,
    *,
    completed_steps: list[str],
    requested_labels: list[str],
    requested_parent_issue_number: int,
    requested_dependency_issue_numbers: list[int],
    requested_blocking_issue_numbers: list[int] | None = None,
    failure_context: str | None = None,
) -> None:
    requested_blocking_issue_numbers = requested_blocking_issue_numbers or []
    requested_labels_text = ", ".join(requested_labels) if requested_labels else "(none)"
    requested_parent_text = f"#{requested_parent_issue_number}" if requested_parent_issue_number else "(none)"
    requested_dependencies_text = (
        ", ".join(f"#{int(dep)}" for dep in requested_dependency_issue_numbers)
        if requested_dependency_issue_numbers
        else "(none)"
    )
    requested_blocking_text = (
        ", ".join(f"#{int(dep)}" for dep in requested_blocking_issue_numbers)
        if requested_blocking_issue_numbers
        else "(none)"
    )
    completed_steps_text = ", ".join(completed_steps) if completed_steps else "(none)"

    recovery_hint = _recovery_hint_for_stage(
        failed_stage=failed_stage,
        repo=repo,
        issue_number=issue_number,
        requested_parent_issue_number=requested_parent_issue_number,
        requested_dependency_issue_numbers=requested_dependency_issue_numbers,
        requested_blocking_issue_numbers=requested_blocking_issue_numbers,
    )

    comment = (
        "create-issue transaction partial-failure\n\n"
        f"Issue: #{issue_number}\n"
        f"Failure stage: {failed_stage}\n"
        f"Message: {failure_message}\n\n"
        "Requested:\n"
        f"- labels: {requested_labels_text}\n"
        f"- parent: {requested_parent_text}\n"
        f"- blocked-by (dependencies): {requested_dependencies_text}\n"
        f"- blocking: {requested_blocking_text}\n"
        f"Completed steps: {completed_steps_text}\n\n"
        f"Failure context: {failure_context or '(none)'}\n\n"
        f"{recovery_hint}\n\n"
        "Please recover deterministically before re-running the create issue transaction."
    )
    args = [
        gh_bin,
        "issue",
        "comment",
        "--repo",
        repo,
        str(issue_number),
        "--body",
        comment,
    ]
    cp = run_command(args)
    if cp.returncode != 0:
        raise TransactionError(
            stage="partial-failure-comment",
            message="partial-failure audit comment failed",
            output=(cp.stderr or cp.stdout).strip(),
        )


def _report_partial_failure(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    failed_exc: TransactionError,
    completed_steps: list[str],
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int],
    gh_bin: str,
    parent_verified: bool | None,
    dependency_verified: bool | None,
    blocking_issue_numbers: list[int] | None = None,
    blocking_verified: bool | None = None,
    failure_context: str | None = None,
    applied_steps: list[str] | None = None,
    verified_steps: list[str] | None = None,
    pending_steps: list[str] | None = None,
    failed_readbacks: list[dict] | None = None,
) -> TransactionResult:
    blocking_issue_numbers = blocking_issue_numbers or []
    failure_stage = failed_exc.stage
    failure_message = failed_exc.message
    audit_comment_posted: bool = True
    try:
        _post_partial_failure_comment(
            repo=repo,
            issue_number=issue_number,
            failed_stage=failed_exc.stage,
            failure_message=failed_exc.message,
            gh_bin=gh_bin,
            completed_steps=completed_steps,
            requested_labels=labels,
            requested_parent_issue_number=parent_issue_number,
            requested_dependency_issue_numbers=dependency_issue_numbers,
            requested_blocking_issue_numbers=blocking_issue_numbers,
            failure_context="\n".join(
                [
                    line
                    for line in [
                        f"command={failed_exc.command}",
                        f"output={failed_exc.output}",
                    ]
                    if line != "output=None" and line != "command=None"
                ]
            )
            + (f"\n{failure_context}" if failure_context else ""),
        )
    except TransactionError as comment_error:
        audit_comment_posted = False
        failure_stage = comment_error.stage
        failure_message = (
            f"{failed_exc.message}; audit-comment-failed={comment_error.message} output={comment_error.output}"
        )

    return TransactionResult(
        status="partial_failure",
        issue_number=issue_number,
        issue_url=issue_url,
        completed_steps=completed_steps,
        failure_stage=failure_stage,
        failure_message=failure_message,
        parent_verified=parent_verified,
        dependency_verified=dependency_verified,
        blocking_verified=blocking_verified,
        audit_comment_posted=audit_comment_posted,
        # Blocker C: pass through step tracking fields so all return paths are consistent.
        applied_steps=applied_steps if applied_steps is not None else [],
        verified_steps=verified_steps if verified_steps is not None else [],
        pending_steps=pending_steps if pending_steps is not None else [],
        failed_readbacks=failed_readbacks if failed_readbacks is not None else [],
    )


def _reconcile_issue_links(
    repo: str,
    issue_number: int,
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int],
    gh_bin: str,
    *,
    blocking_issue_numbers: list[int] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[list[str], bool | None, bool | None, bool | None, list[dict]]:
    blocking_issue_numbers = blocking_issue_numbers or []
    completed: list[str] = []
    parent_verified = True if parent_issue_number else None
    dependency_verified = True if dependency_issue_numbers else None
    blocking_verified = True if blocking_issue_numbers else None

    failed_readbacks_reconcile: list[dict] = []
    if labels:
        labels_ok = _readback_labels(repo, issue_number, labels, gh_bin, sleep_fn=sleep_fn)
        if labels_ok:
            completed.append("label-readback")
        else:
            # collect-and-reconcile: record the failure but proceed with sub-issue registration
            # Use _readback_labels_with_result to get actual_labels for diagnostics
            label_rb = _readback_labels_with_result(repo, issue_number, labels, gh_bin, sleep_fn=sleep_fn)
            failed_readbacks_reconcile.append({
                "stage": "dedupe-label-readback",
                "expected_labels": label_rb.expected_labels,
                "actual_labels": label_rb.actual_labels,
                "attempts": label_rb.attempts,
                "retry_delays": label_rb.retry_delays,
                "error_kind": label_rb.error_kind,
                "message": "label read-back mismatch after retries (dedupe path)",
            })

    if parent_issue_number:
        # Attempt idempotent sub-issue registration for dedupe path.
        # If already registered, _issue_register_sub_issue_idempotent returns "already_registered" (PASS).
        # If registration fails non-idempotently, it raises TransactionError.
        _, child_db_id = _issue_graphql_ids(repo, issue_number, gh_bin)
        _issue_register_sub_issue_idempotent(
            repo, parent_issue_number, child_db_id, issue_number, gh_bin
        )
        completed.append("sub_issue")
        parent_verified = _readback_parent_issue_with_retry(
            repo, issue_number, parent_issue_number, gh_bin, sleep_fn=sleep_fn
        )
        if not parent_verified:
            raise TransactionError(stage="sub-issue-readback", message="parent read-back mismatch")
        completed.append("sub-issue-readback")

    if dependency_issue_numbers:
        # #1946 Owner P0-1: the previous implementation only read back the --blocked-by
        # direction here and never mutated it, so a dedupe issue missing the link was
        # silently left unregistered. _apply_relationship_direction computes the diff
        # against the CURRENT state and mutates only what is missing.
        dep_apply = _apply_relationship_direction(
            repo,
            issue_number,
            list(dependency_issue_numbers),
            "blocked_by",
            gh_bin,
            sleep_fn=sleep_fn,
        )
        if dep_apply.mutated_numbers:
            completed.append("dependency")
        dependency_verified = dep_apply.verified
        if not dependency_verified:
            raise TransactionError(stage="dependency-readback", message="dependency (blocked-by) read-back mismatch")
        completed.append("dependency-readback")

    if blocking_issue_numbers:
        # #1946 Owner P0-1: same fix as above, mirrored for the --blocking direction.
        blk_apply = _apply_relationship_direction(
            repo,
            issue_number,
            list(blocking_issue_numbers),
            "blocking",
            gh_bin,
            sleep_fn=sleep_fn,
        )
        if blk_apply.mutated_numbers:
            completed.append("blocking")
        blocking_verified = blk_apply.verified
        if not blocking_verified:
            raise TransactionError(stage="blocking-readback", message="blocking read-back mismatch")
        completed.append("blocking-readback")

    return completed, parent_verified, dependency_verified, blocking_verified, failed_readbacks_reconcile


def _run_issue_body_validator(
    body: str, issue_kind: str = "", title: str | None = None
) -> dict[str, Any]:
    """Call validate_issue_body.py validator on issue body text.

    Returns the JSON output from the validator (parsed).
    Raises on validator exit code != 0/1 (internal error case exit 2 or unknown code).
    """
    import subprocess
    import json
    import tempfile

    # Write body to temporary file for validator (with explicit UTF-8 encoding)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.md', delete=False) as f:
        f.write(body)
        temp_body_file = f.name

    try:
        validator_script = Path(__file__).parent / "validate_issue_body.py"
        if not validator_script.exists():
            raise TransactionError(
                stage="issue-body-validate",
                message=f"validate_issue_body.py not found at {validator_script}"
            )

        validator_args = [
            sys.executable,
            str(validator_script),
            "--body-file",
            temp_body_file,
        ]
        # AC3 + High1 (#946): forward kind/title so the validator enforces kind-specific
        # required sections / Stop Conditions / title prefix even when the caller forgot to
        # pre-validate. When --issue-kind is omitted, adopt the body Machine-Readable
        # Contract issue_kind; an explicit --issue-kind that contradicts the MRC kind is
        # fail-closed.
        mrc_kind = _extract_field_from_body(body, "issue_kind") or ""
        if issue_kind and mrc_kind and issue_kind != mrc_kind:
            raise TransactionError(
                stage="issue-body-validate",
                message=(
                    f"issue_kind mismatch: --issue-kind={issue_kind!r} but body MRC "
                    f"issue_kind={mrc_kind!r}"
                ),
            )
        effective_kind = issue_kind or mrc_kind
        if effective_kind:
            validator_args += ["--kind", effective_kind]
        if title:
            validator_args += ["--title", title]
        cp = subprocess.run(
            validator_args,
            capture_output=True,
            text=True
        )

        # Parse validator JSON output
        try:
            result = json.loads(cp.stdout)
        except json.JSONDecodeError as exc:
            raise TransactionError(
                stage="issue-body-validate",
                message=f"Validator returned non-JSON output: {exc}",
                output=cp.stdout[:500]
            )

        # Validator exit codes: 0 = pass, 1 = fail (with errors in JSON)
        # Any other exit code (including 2 for internal error) is fail-closed
        if cp.returncode not in (0, 1):
            raise TransactionError(
                stage="issue-body-validate",
                message=f"Validator error (exit code {cp.returncode})",
                output=cp.stderr[:500] if cp.stderr else cp.stdout[:500]
            )

        return result

    finally:
        # Clean up temp file
        Path(temp_body_file).unlink(missing_ok=True)


def _run_transaction_engine(
    *,
    repo: str,
    title: str,
    body: str,
    body_file: str,
    labels: list[str],
    issue_kind: str = "",
    label_profile: str = "standard",
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    gh_bin: str,
    blocking_issue_numbers: list[int | str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    skip_internal_title_dedupe: bool = False,
    approved_operations_only: bool = False,
) -> TransactionResult:
    """Source create transaction (verbatim semantics; label/parent/dependency handlers retained).

    Stage 2-5: NOT an entrypoint. Only `run_transaction` (the gated public function) may call it
    with ``approved_operations_only=True``. The default False exists so the retained source
    handlers stay testable with a fake `gh`; no CLI/library path reaches them with False.
    """
    labels = _resolve_labels(labels, issue_kind, label_profile)
    try:
        normalized_dependency_issue_numbers = _normalize_dependency_numbers(
            dependency_issue_numbers, label="--blocked-by"
        )
        normalized_blocking_issue_numbers = _normalize_dependency_numbers(
            blocking_issue_numbers or [], label="--blocking"
        )
        _validate_dependency_directions(
            normalized_dependency_issue_numbers, normalized_blocking_issue_numbers
        )
    except TransactionError as exc:
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    completed: list[str] = []

    # --- Blocker 2: body_file fail-closed check (before any GitHub mutation) ---
    if body_file:
        _bf_path = Path(body_file)
        if not _bf_path.is_file():
            return TransactionResult(
                status="failure",
                issue_number=None,
                issue_url=None,
                completed_steps=[],
                failure_stage="body-file-read",
                failure_message=f"body_file not found: {body_file}",
            )
        try:
            _body_text_for_parent = _bf_path.read_text(encoding="utf-8")
        except OSError as exc:
            return TransactionResult(
                status="failure",
                issue_number=None,
                issue_url=None,
                completed_steps=[],
                failure_stage="body-file-read",
                failure_message=str(exc),
            )
    else:
        _body_text_for_parent = body
    # --- End Blocker 2 ---

    # --- Blocker 2.5: Issue body validation (fail-closed before any GitHub mutation) ---
    # Call validate_issue_body.py to check for LP rule violations
    try:
        _validator_result = _run_issue_body_validator(
            _body_text_for_parent, issue_kind=issue_kind, title=title
        )
        if _validator_result.get("status") == "fail":
            # Extract error details for logging
            errors = _validator_result.get("errors", [])
            error_count = len(errors)
            error_summary = f"{error_count} validation error(s)" if error_count > 0 else "validation error"
            return TransactionResult(
                status="failure",
                issue_number=None,
                issue_url=None,
                completed_steps=[],
                failure_stage="issue-body-validate",
                failure_message=f"Issue body validation failed: {error_summary}",
            )
    except Exception as exc:
        # Validator execution error (exit code 2 or exception)
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage="issue-body-validate",
            failure_message=f"Issue body validator error: {str(exc)}",
        )
    # --- End Blocker 2.5 ---

    # --- Body-derived parent resolution (fail-closed before any GitHub mutation) ---
    try:
        body_resolution = _extract_parent_resolution_from_body(_body_text_for_parent)
    except TransactionError as exc:
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    try:
        parent_issue_number = _resolve_parent_issue_number(parent_issue_number, body_resolution)
    except TransactionError as exc:
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    # --- End parent resolution ---

    # Stage 2-5: a parent resolved from the BODY is still an (unapproved) sub-issue mutation.
    # Refuse here -- after local body parsing, before the first `gh` call of any kind.
    if approved_operations_only and parent_issue_number:
        return _unapproved_operation_result(
            "parent",
            f"body-derived or explicit parent #{parent_issue_number} would register a sub-issue",
        )

    # skip_internal_title_dedupe (Issue #2602 AC2(g)): opt-in bypass of this script's own
    # internal title-only OPEN-issue dedupe search. Intended ONLY for callers that have
    # already confirmed via an outer, dedupe_key-based search (e.g.
    # plan_child_materialization.py::_search_dedupe_candidates() /
    # docs/dev/agent-skill-boundaries.md FOLLOW_UP_ISSUE_REQUEST_V1 dedupe flow) that no
    # duplicate exists across OPEN/CLOSED issues. Without this bypass, this internal
    # title-only search could re-match a DIFFERENT issue that merely shares the same
    # title (dedupe_key mismatch), silently overriding the outer caller's "create
    # confirmed" decision. Default is False: unchanged behavior for all existing callers.
    if skip_internal_title_dedupe:
        dedupe_issue_numbers = []
    else:
        try:
            dedupe_issue_numbers = _find_open_issues_by_title(repo, title, gh_bin)
        except TransactionError as exc:
            return TransactionResult(
                status="failure",
                issue_number=None,
                issue_url=None,
                completed_steps=[],
                failure_stage=exc.stage,
                failure_message=exc.message,
            )
    dedupe_number: int | None = dedupe_issue_numbers[0] if dedupe_issue_numbers else None
    if len(dedupe_issue_numbers) > 1:
        dedupe_collision_issue_number = dedupe_issue_numbers[0]
        dedupe_failure = TransactionError(
            stage="dedupe-search",
            message="multiple exact-title open issues before create",
            output=str(dedupe_issue_numbers),
        )
        return _report_partial_failure(
            repo=repo,
            issue_number=dedupe_collision_issue_number,
            issue_url=f"https://github.com/{repo}/issues/{dedupe_collision_issue_number}",
            failed_exc=dedupe_failure,
            completed_steps=["dedupe"],
            labels=labels,
            parent_issue_number=parent_issue_number,
            dependency_issue_numbers=normalized_dependency_issue_numbers,
            blocking_issue_numbers=normalized_blocking_issue_numbers,
            gh_bin=gh_bin,
            parent_verified=None,
            dependency_verified=None,
            failure_context=f"candidate_issue_numbers={dedupe_issue_numbers}",
        )

    if dedupe_number is not None:
        dedupe_completed = ["dedupe"]
        parent_verified = None
        dependency_verified = None
        blocking_verified = None
        dedupe_issue_url = f"https://github.com/{repo}/issues/{dedupe_number}"

        # AC3: reject self-reference before any mutation -- the dedupe target issue
        # cannot be its own --blocked-by/--blocking entry.
        try:
            _validate_dependency_directions(
                normalized_dependency_issue_numbers,
                normalized_blocking_issue_numbers,
                self_issue_number=dedupe_number,
            )
        except TransactionError as exc:
            return TransactionResult(
                status="failure",
                issue_number=dedupe_number,
                issue_url=dedupe_issue_url,
                completed_steps=dedupe_completed,
                failure_stage=exc.stage,
                failure_message=exc.message,
            )

        # --- Blocker 3 + 4 + iteration 3: dedupe body read for identity checks ---
        # Always read existing body when parent or issue_kind checks are needed.
        # Body read failure is fail-closed only when parent_issue_number is set;
        # for issue_kind (best-effort) we silently skip on read failure.
        _dedupe_read_needed = bool(parent_issue_number) or bool(_body_text_for_parent)
        existing_body = ""
        if _dedupe_read_needed:
            try:
                dedupe_issue_data = _run_gh_json(
                    [
                        gh_bin,
                        "api",
                        "--method",
                        "GET",
                        "-H",
                        "Accept: application/vnd.github+json",
                        "-H",
                        "X-GitHub-Api-Version: 2022-11-28",
                        f"/repos/{repo}/issues/{dedupe_number}",
                    ],
                    stage="dedupe-body-read",
                )
                existing_body = dedupe_issue_data.get("body") or ""
            except TransactionError as exc:
                if parent_issue_number:
                    # Fail-closed when parent checks are required.
                    return TransactionResult(
                        status="failure",
                        issue_number=dedupe_number,
                        issue_url=dedupe_issue_url,
                        completed_steps=dedupe_completed,
                        failure_stage="dedupe-body-read",
                        failure_message=exc.message,
                    )
                # For issue_kind-only check: best-effort; skip on read failure.
                existing_body = ""

        if parent_issue_number and existing_body is not None:
            # Blocker 4: existing body parse error is fail-closed (not silenced).
            try:
                existing_body_resolution = _extract_parent_resolution_from_body(existing_body)
            except TransactionError as exc:
                return TransactionResult(
                    status="failure",
                    issue_number=dedupe_number,
                    issue_url=dedupe_issue_url,
                    completed_steps=dedupe_completed,
                    failure_stage="dedupe-parent-body-parse",
                    failure_message=exc.message,
                )
            # Blocker 2 (iteration 3): existing body declares explicit_none but current
            # transaction resolved a parent -> fail-closed to avoid silent link mutation.
            if existing_body_resolution.state == "explicit_none":
                return TransactionResult(
                    status="failure",
                    issue_number=dedupe_number,
                    issue_url=dedupe_issue_url,
                    completed_steps=dedupe_completed,
                    failure_stage="dedupe-parent-mismatch",
                    failure_message=(
                        f"dedupe issue #{dedupe_number} explicitly declares no parent, "
                        f"but current transaction resolved parent #{parent_issue_number}; "
                        "failing closed to avoid link mutation."
                    ),
                )
            # If existing issue body has a conflicting numeric parent, fail-closed.
            if existing_body_resolution.state == "number":
                existing_body_parent = existing_body_resolution.value
                if existing_body_parent != parent_issue_number:
                    return TransactionResult(
                        status="failure",
                        issue_number=dedupe_number,
                        issue_url=dedupe_issue_url,
                        completed_steps=dedupe_completed,
                        failure_stage="dedupe-parent-mismatch",
                        failure_message=(
                            f"dedupe issue #{dedupe_number} body declares parent #{existing_body_parent} "
                            f"but current transaction resolved parent #{parent_issue_number}; "
                            "failing closed to avoid link mutation."
                        ),
                    )

        # High (iteration 3): identity gate — issue_kind mismatch check (best-effort).
        # Skip if existing body could not be read (best-effort: None means skip the gate).
        if existing_body:
            existing_issue_kind = _extract_field_from_body(existing_body, "issue_kind")
            expected_issue_kind = _extract_field_from_body(_body_text_for_parent, "issue_kind")
            if existing_issue_kind and expected_issue_kind and existing_issue_kind != expected_issue_kind:
                return TransactionResult(
                    status="failure",
                    issue_number=dedupe_number,
                    issue_url=dedupe_issue_url,
                    completed_steps=dedupe_completed,
                    failure_stage="dedupe-kind-mismatch",
                    failure_message=(
                        f"dedupe issue #{dedupe_number} has issue_kind={existing_issue_kind!r} "
                        f"but current transaction expects issue_kind={expected_issue_kind!r}; "
                        "failing closed to avoid identity collision."
                    ),
                )
        # --- End Blocker 3 + 4 + iteration 3 ---

        try:
            (
                reconcile_steps,
                parent_verified,
                dependency_verified,
                blocking_verified,
                reconcile_failed_readbacks,
            ) = _reconcile_issue_links(
                repo=repo,
                issue_number=dedupe_number,
                labels=labels,
                parent_issue_number=parent_issue_number,
                dependency_issue_numbers=normalized_dependency_issue_numbers,
                gh_bin=gh_bin,
                blocking_issue_numbers=normalized_blocking_issue_numbers,
                sleep_fn=sleep_fn,
            )
            dedupe_completed.extend(reconcile_steps)
            if reconcile_failed_readbacks:
                # partial_failure: label readback failed but sub-issue registration succeeded
                _dedupe_audit_comment_posted: bool = True
                try:
                    _post_partial_failure_comment(
                        repo=repo,
                        issue_number=dedupe_number,
                        failed_stage=reconcile_failed_readbacks[0]["stage"],
                        failure_message=reconcile_failed_readbacks[0]["message"],
                        gh_bin=gh_bin,
                        completed_steps=dedupe_completed,
                        requested_labels=labels,
                        requested_parent_issue_number=parent_issue_number,
                        requested_dependency_issue_numbers=normalized_dependency_issue_numbers,
                        requested_blocking_issue_numbers=normalized_blocking_issue_numbers,
                    )
                except TransactionError:
                    _dedupe_audit_comment_posted = False
                return TransactionResult(
                    status="partial_failure",
                    issue_number=dedupe_number,
                    issue_url=dedupe_issue_url,
                    completed_steps=dedupe_completed,
                    dedupe_number=dedupe_number,
                    failure_stage=reconcile_failed_readbacks[0]["stage"],
                    failure_message=reconcile_failed_readbacks[0]["message"],
                    parent_verified=parent_verified,
                    dependency_verified=dependency_verified,
                    blocking_verified=blocking_verified,
                    failed_readbacks=reconcile_failed_readbacks,
                    audit_comment_posted=_dedupe_audit_comment_posted,
                )
            return TransactionResult(
                status="dedupe",
                issue_number=dedupe_number,
                issue_url=dedupe_issue_url,
                completed_steps=dedupe_completed,
                dedupe_number=dedupe_number,
                parent_verified=parent_verified,
                dependency_verified=dependency_verified,
                blocking_verified=blocking_verified,
            )
        except TransactionError as exc:
            return _report_partial_failure(
                repo=repo,
                issue_number=dedupe_number,
                issue_url=dedupe_issue_url,
                failed_exc=exc,
                completed_steps=dedupe_completed,
                labels=labels,
                parent_issue_number=parent_issue_number,
                dependency_issue_numbers=normalized_dependency_issue_numbers,
                blocking_issue_numbers=normalized_blocking_issue_numbers,
                gh_bin=gh_bin,
                parent_verified=parent_verified,
                dependency_verified=dependency_verified,
                blocking_verified=blocking_verified,
            )

    try:
        issue_url = _issue_create(repo, title, body, body_file, gh_bin)
        issue_number = _issue_number_from_url(issue_url)
    except TransactionError as exc:
        return _create_failure_result(
            repo, title, exc, gh_bin, attribute_candidate=not skip_internal_title_dedupe
        )
    completed.append("create")
    matching_issue_numbers: list[int] = []

    # Blocker C: initialize step tracking fields before the try block so that all
    # exception exit paths (including early exits before label-apply) can pass
    # consistently populated values to _report_partial_failure.
    applied_steps: list[str] = []
    verified_steps: list[str] = []
    pending_steps: list[str] = []
    failed_readbacks: list[dict] = []

    # #1946 Owner P1-2: initialize each verification state OUTSIDE the try block so an
    # exception raised partway through (e.g. --blocked-by succeeds, --blocking then fails)
    # carries the TRUE partial-progress state into the except handler below, instead of
    # a hardcoded None/None/None that contradicts verified_steps.
    #   None  -> not attempted
    #   False -> readback attempted but did not match
    #   True  -> readback confirmed
    parent_verified: bool | None = None
    dependency_verified: bool | None = None
    blocking_verified: bool | None = None

    try:
        # AC3: reject self-reference before any dependency mutation -- the newly
        # created issue cannot be its own --blocked-by/--blocking entry. This can only
        # be checked now, after create, because the issue number is assigned by GitHub.
        _validate_dependency_directions(
            normalized_dependency_issue_numbers,
            normalized_blocking_issue_numbers,
            self_issue_number=issue_number,
        )
        try:
            poll_verdict, matching_issue_numbers = _poll_for_created_issue(
                repo,
                title,
                issue_number,
                gh_bin,
                sleep_fn=sleep_fn,
                skip_internal_title_dedupe=skip_internal_title_dedupe,
            )
        except TransactionError as exc:
            if exc.stage == "dedupe-search":
                raise TransactionError(
                    stage="dedupe-race-detection",
                    message=exc.message,
                    command=exc.command,
                    output=exc.output,
                ) from exc
            raise

        if poll_verdict == "race":
            raise TransactionError(
                stage="dedupe-race-detection",
                message="duplicate open issue title collision after create",
                output=f"matching={matching_issue_numbers}",
            )

        if poll_verdict == "inconclusive":
            n_attempts = _DEFAULT_MAX_ATTEMPTS
            sys.stderr.write(
                f"[WARN] create_issue_txn: post-create race detection inconclusive after {n_attempts} attempts"
                f" (search index propagation delay)."
                f" issue=#{issue_number} title={title}."
                " Manual verification recommended.\n"
            )
            _observability_comment = (
                f"⚠️ create-issue transaction: post-create race detection was inconclusive"
                f" due to GitHub search index propagation delay (polled {n_attempts} attempts)."
                " 同タイトルの open issue が後から見えた場合は手動でクローズ判定してください。"
            )
            _obs_args = [
                gh_bin,
                "issue",
                "comment",
                "--repo",
                repo,
                str(issue_number),
                "--body",
                _observability_comment,
            ]
            run_command(_obs_args)  # best-effort; ignore failure
            completed.append("race-detection-inconclusive")

        # Fresh remote title/body readback (Issue #35 AC2). The baseline is the body text
        # snapshotted before the create; the issue already exists here, so a mismatch is a
        # partial_failure (never failed_no_mutation) that keeps issue_number/issue_url.
        try:
            _readback_created_issue(
                repo, issue_number, issue_url, title, _body_text_for_parent, gh_bin, sleep_fn=sleep_fn
            )
        except TransactionError:
            pending_steps.append("body-readback")
            raise
        completed.append("body-readback")
        verified_steps.append("body-readback")

        _issue_apply_labels(repo, issue_number, labels, gh_bin)
        completed.append("label")
        # Blocker C: update (not re-declare) applied_steps, which was initialized before the try block.
        if labels:
            applied_steps.append("label")
        if labels:
            labels_verified = _readback_labels(repo, issue_number, labels, gh_bin, sleep_fn=sleep_fn)
            if labels_verified:
                completed.append("label-readback")
                verified_steps.append("label-readback")
            else:
                # collect-and-reconcile: record the failure but do NOT raise
                # sub-issue registration must proceed regardless of label readback result
                # Use _readback_labels_with_result to get actual_labels for diagnostics
                label_rb_result = _readback_labels_with_result(repo, issue_number, labels, gh_bin, sleep_fn=sleep_fn)
                failed_readbacks.append({
                    "stage": "label-readback",
                    "expected_labels": label_rb_result.expected_labels,
                    "actual_labels": label_rb_result.actual_labels,
                    "attempts": label_rb_result.attempts,
                    "retry_delays": label_rb_result.retry_delays,
                    "error_kind": label_rb_result.error_kind,
                    "message": "label read-back mismatch after retries",
                })
                pending_steps.append("label-readback")

        child_node_id = ""
        child_db_id: int | None = None

        if normalized_dependency_issue_numbers or normalized_blocking_issue_numbers:
            child_node_id, child_db_id = _issue_graphql_ids(repo, issue_number, gh_bin)

        if parent_issue_number:
            if child_db_id is None:
                _, child_db_id = _issue_graphql_ids(repo, issue_number, gh_bin)
            _issue_register_sub_issue_idempotent(
                repo, parent_issue_number, child_db_id, issue_number, gh_bin
            )
            completed.append("sub_issue")
            applied_steps.append("sub_issue")

        if normalized_dependency_issue_numbers:
            # #1946 Owner P0-1: shared apply/readback helper (also used by dedupe/reconcile).
            # assume_current_empty=True is safe here: the issue was just created, so it
            # cannot already have any blockedBy relationships.
            dep_apply = _apply_relationship_direction(
                repo,
                issue_number,
                normalized_dependency_issue_numbers,
                "blocked_by",
                gh_bin,
                assume_current_empty=True,
                sleep_fn=sleep_fn,
            )
            if dep_apply.mutated_numbers:
                completed.append("dependency")
                applied_steps.append("dependency")

            dependency_verified = dep_apply.verified
            if not dependency_verified:
                raise TransactionError(stage="dependency-readback", message="dependency read-back mismatch")

            completed.append("dependency-readback")
            verified_steps.append("dependency-readback")

        if normalized_blocking_issue_numbers:
            # #1946 Owner P0-1: same shared helper, mirrored for --blocking.
            blk_apply = _apply_relationship_direction(
                repo,
                issue_number,
                normalized_blocking_issue_numbers,
                "blocking",
                gh_bin,
                assume_current_empty=True,
                sleep_fn=sleep_fn,
            )
            if blk_apply.mutated_numbers:
                completed.append("blocking")
                applied_steps.append("blocking")

            blocking_verified = blk_apply.verified
            if not blocking_verified:
                raise TransactionError(stage="blocking-readback", message="blocking read-back mismatch")

            completed.append("blocking-readback")
            verified_steps.append("blocking-readback")

        if parent_issue_number:
            parent_verified = _readback_parent_issue_with_retry(
                repo, issue_number, parent_issue_number, gh_bin, sleep_fn=sleep_fn
            )
            if not parent_verified:
                raise TransactionError(
                    stage="sub-issue-readback",
                    message="parent read-back mismatch",
                )
            completed.append("sub-issue-readback")
            verified_steps.append("sub-issue-readback")

        if failed_readbacks:
            audit_comment_posted: bool | None = None
            try:
                _post_partial_failure_comment(
                    repo=repo,
                    issue_number=issue_number,
                    failed_stage=failed_readbacks[0]["stage"],
                    failure_message=failed_readbacks[0]["message"],
                    gh_bin=gh_bin,
                    completed_steps=completed,
                    requested_labels=labels,
                    requested_parent_issue_number=parent_issue_number,
                    requested_dependency_issue_numbers=normalized_dependency_issue_numbers,
                    requested_blocking_issue_numbers=normalized_blocking_issue_numbers,
                )
                audit_comment_posted = True
            except TransactionError:
                audit_comment_posted = False  # comment failed; track it
            return TransactionResult(
                status="partial_failure",
                issue_number=issue_number,
                issue_url=issue_url,
                completed_steps=completed,
                failure_stage=failed_readbacks[0]["stage"],
                failure_message=failed_readbacks[0]["message"],
                parent_verified=parent_verified if parent_issue_number else None,
                dependency_verified=dependency_verified if normalized_dependency_issue_numbers else None,
                blocking_verified=blocking_verified if normalized_blocking_issue_numbers else None,
                failed_readbacks=failed_readbacks,
                applied_steps=applied_steps,
                verified_steps=verified_steps,
                pending_steps=pending_steps,
                audit_comment_posted=audit_comment_posted,
            )

        return TransactionResult(
            status="success",
            issue_number=issue_number,
            issue_url=issue_url,
            completed_steps=completed,
            parent_verified=parent_verified if parent_issue_number else None,
            dependency_verified=dependency_verified if normalized_dependency_issue_numbers else None,
            blocking_verified=blocking_verified if normalized_blocking_issue_numbers else None,
            applied_steps=applied_steps,
            verified_steps=verified_steps,
            pending_steps=pending_steps,
        )

    except TransactionError as exc:
        return _report_partial_failure(
            repo=repo,
            issue_number=issue_number,
            issue_url=issue_url,
            failed_exc=exc,
            completed_steps=completed,
            labels=labels,
            parent_issue_number=parent_issue_number,
            dependency_issue_numbers=normalized_dependency_issue_numbers,
            blocking_issue_numbers=normalized_blocking_issue_numbers,
            gh_bin=gh_bin,
            # #1946 Owner P1-2: pass through the ACTUAL partial-progress state (tracked
            # outside the try block above) instead of hardcoded None, so a receipt like
            # "dependency-readback completed but blocking-readback failed" is represented
            # faithfully (dependency_verified=True, blocking_verified=False) rather than
            # both collapsing to null.
            parent_verified=parent_verified,
            dependency_verified=dependency_verified,
            blocking_verified=blocking_verified,
            failure_context="\n".join(
                [
                    f"matching_issue_numbers={matching_issue_numbers}",
                    f"issue_number={issue_number}",
                ]
            ),
            # Blocker C: pass through step tracking fields so all exception paths are consistent.
            applied_steps=applied_steps,
            verified_steps=verified_steps,
            pending_steps=pending_steps,
            failed_readbacks=failed_readbacks,
        )


def _require_trusted_repo(repo: object) -> None:
    """Fail closed unless `repo` equals the trusted slug from repo_identity (no `gh` call yet).

    Loaded from the fixed on-disk path on every call (no sys.path / sys.modules lookup), so a
    shadowing or pre-registered `repo_identity` module can never answer for the trusted slug.
    An unusable identity config/module is a rejection, never a skip.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_create_issue_txn_repo_identity", AGENT_GUARDS_IMPORT_ROOT / "repo_identity.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("repo_identity_spec_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        trusted = module.is_trusted_repo(repo)
    except Exception as exc:
        raise TransactionError(
            stage="repo-identity", message="repo_identity_unavailable"
        ) from exc
    if not trusted:
        raise TransactionError(stage="repo-identity", message="repo_untrusted")


def _unapproved_operation_result(
    operation: str, detail: str, issue_number: int | None = None
) -> TransactionResult:
    return TransactionResult(
        status="failure",
        issue_number=issue_number,
        issue_url=None,
        completed_steps=[],
        failure_stage="unapproved-operation",
        failure_message=(
            f"{operation} mutation is not approved for agent-platform "
            f"(rejected before any gh call): {detail}"
        ),
    )


def _gate_approved_operations(
    *,
    repo: object,
    labels: list[str],
    issue_kind: str,
    label_profile: str,
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    blocking_issue_numbers: list[int | str] | None,
    issue_number: int | None = None,
) -> TransactionResult | None:
    """Stage 2-5 pre-flight: refuse repo / label / parent / dependency before any `gh` call.

    Label-free creation is `label_profile="standard"` + a kind that auto-assigns nothing (any
    kind except an explicit `--issue-kind implementation`) + no explicit label. `triage_only`
    is NOT label-free: `_resolve_labels` always adds `triage-required`.
    """
    try:
        _require_trusted_repo(repo)
    except TransactionError as exc:
        return TransactionResult(
            status="failure",
            issue_number=issue_number,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    effective_labels = _resolve_labels(list(labels), issue_kind, label_profile)
    if effective_labels:
        return _unapproved_operation_result(
            "label",
            f"effective labels {effective_labels} (label_profile={label_profile!r}, "
            f"issue_kind={issue_kind!r})",
            issue_number,
        )
    if parent_issue_number:
        return _unapproved_operation_result("parent", f"parent #{parent_issue_number}", issue_number)
    if dependency_issue_numbers:
        return _unapproved_operation_result("blocked-by", "dependency input", issue_number)
    if blocking_issue_numbers:
        return _unapproved_operation_result("blocking", "blocking input", issue_number)
    return None


def _create_failure_result(
    repo: str, title: str, exc: TransactionError, gh_bin: str, *, attribute_candidate: bool = True
) -> TransactionResult:
    """Classify a failure of the `gh issue create` step without claiming 'no mutation'.

    Every failure except a pre-create `body-file not found` leaves the write outcome uncertain
    (timeout, lost receipt, a non-zero exit after the write was applied). It is reconciled by an
    authoritative title readback: a visible candidate or an unavailable readback is reported as
    `issue-create-outcome-unknown`; only a definite non-zero exit with a successful readback that
    shows no such issue stays the plain `issue-create` failure (still without a no-mutation claim,
    because the search index may lag).
    """
    if exc.message == "body-file not found":
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    candidates: list[int] | None
    try:
        candidates = _find_open_issues_by_title(repo, title, gh_bin)
    except TransactionError:
        candidates = None
    definite_exit = exc.stage == "issue-create" and exc.message == "issue-create failed"
    if definite_exit and candidates == []:
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage="issue-create",
            failure_message=(
                "issue-create failed; title readback found no open issue "
                "(search index may lag, so no-mutation is not asserted)"
            ),
        )
    if candidates is None:
        readback = "title readback unavailable"
    elif candidates:
        readback = f"open issues with the exact title: {candidates}"
    else:
        readback = "title readback found no open issue (search index may lag)"
    return TransactionResult(
        status="failure",
        # With skip_internal_title_dedupe the title match may be an unrelated same-title issue,
        # so it is reported in the message only, never attributed as this create's issue_number.
        issue_number=(
            candidates[0] if attribute_candidate and candidates and len(candidates) == 1 else None
        ),
        issue_url=None,
        completed_steps=[],
        failure_stage="issue-create-outcome-unknown",
        failure_message=(
            f"issue-create outcome unknown ({exc.stage}: {exc.message}); {readback}. "
            "Do not assume no mutation; verify before retrying."
        ),
    )


def _inconclusive_readback_is_not_success(result: TransactionResult) -> TransactionResult:
    """A create whose post-create readback never became conclusive is not a verified success.

    The issue exists (issue_number/url kept), but the fresh title readback never confirmed it
    within the bounded poll, so it is reported as `partial_failure` (exit 2), not `success`.
    """
    if result.status == "success" and "race-detection-inconclusive" in result.completed_steps:
        return _dataclass_replace(
            result,
            status="partial_failure",
            failure_stage="race-detection-inconclusive",
            failure_message=(
                "issue created but the post-create readback stayed inconclusive "
                "(search index lag); verify the issue and its title uniqueness manually"
            ),
            pending_steps=[*result.pending_steps, "readback-confirmation"],
        )
    return result


def run_transaction(
    *,
    repo: str,
    title: str,
    body: str,
    body_file: str,
    labels: list[str],
    issue_kind: str = "",
    label_profile: str = "standard",
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    gh_bin: str,
    blocking_issue_numbers: list[int | str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    skip_internal_title_dedupe: bool = False,
) -> TransactionResult:
    """Gated create entrypoint (Stage 2-5): trusted repo + approved operations only."""
    rejected = _gate_approved_operations(
        repo=repo,
        labels=labels,
        issue_kind=issue_kind,
        label_profile=label_profile,
        parent_issue_number=parent_issue_number,
        dependency_issue_numbers=dependency_issue_numbers,
        blocking_issue_numbers=blocking_issue_numbers,
    )
    if rejected is not None:
        return rejected
    result = _run_transaction_engine(
        repo=repo,
        title=title,
        body=body,
        body_file=body_file,
        labels=labels,
        issue_kind=issue_kind,
        label_profile=label_profile,
        parent_issue_number=parent_issue_number,
        dependency_issue_numbers=dependency_issue_numbers,
        gh_bin=gh_bin,
        blocking_issue_numbers=blocking_issue_numbers,
        sleep_fn=sleep_fn,
        skip_internal_title_dedupe=skip_internal_title_dedupe,
        approved_operations_only=True,
    )
    return _inconclusive_readback_is_not_success(result)


def reconcile_transaction(
    *,
    repo: str,
    issue_number: int,
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    gh_bin: str,
    blocking_issue_numbers: list[int | str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TransactionResult:
    """Gated reconcile entrypoint (Stage 2-5).

    Reconcile exists only to re-apply label/parent/dependency state, all of which are
    unapproved, so every approved-operation input is empty by construction and the transaction
    degenerates to a no-op. It is still gated so a wrong repo never reaches `gh`.
    """
    rejected = _gate_approved_operations(
        repo=repo,
        labels=labels,
        issue_kind="",
        label_profile="standard",
        parent_issue_number=parent_issue_number,
        dependency_issue_numbers=dependency_issue_numbers,
        blocking_issue_numbers=blocking_issue_numbers,
        issue_number=issue_number,
    )
    if rejected is not None:
        return rejected
    return _reconcile_transaction_engine(
        repo=repo,
        issue_number=issue_number,
        labels=labels,
        parent_issue_number=parent_issue_number,
        dependency_issue_numbers=dependency_issue_numbers,
        gh_bin=gh_bin,
        blocking_issue_numbers=blocking_issue_numbers,
        sleep_fn=sleep_fn,
    )


def run_transaction_with_authoritative_readback(
    *,
    repo: str,
    title: str,
    body: str,
    body_file: str,
    labels: list[str],
    issue_kind: str = "",
    label_profile: str = "standard",
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    gh_bin: str,
    blocking_issue_numbers: list[int | str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    skip_internal_title_dedupe: bool = False,
) -> TransactionResult:
    """Wrap run_transaction() with an authoritative post-create GraphQL readback
    of node_id, plus a body_sha256 that is set only when run_transaction() verified the stored
    remote body (``body-readback``); it is None otherwise, never a hash of unverified input.

    Salvaged from Issue #2259/PR #2286 AC7 (`node_id`/`body_sha256` readback,
    originally implemented for the bridge dispatch path) and adapted to the
    single native path (Issue #2299 AC4). run_transaction() itself is left
    untouched for existing callers; this wrapper is opt-in.
    """
    result = run_transaction(
        repo=repo,
        title=title,
        body=body,
        body_file=body_file,
        labels=labels,
        issue_kind=issue_kind,
        label_profile=label_profile,
        parent_issue_number=parent_issue_number,
        dependency_issue_numbers=dependency_issue_numbers,
        gh_bin=gh_bin,
        blocking_issue_numbers=blocking_issue_numbers,
        sleep_fn=sleep_fn,
        skip_internal_title_dedupe=skip_internal_title_dedupe,
    )
    if result.issue_number is None:
        return result
    node_id: str | None = None
    try:
        node_id, _ = _issue_graphql_ids(repo, result.issue_number, gh_bin)
    except TransactionError:
        node_id = None
    effective_body: str | None = body
    if body_file:
        try:
            effective_body = Path(body_file).read_text(encoding="utf-8")
        except OSError:
            # The text that was verified against the remote cannot be recovered: claim no hash.
            effective_body = None
    # body_sha256 is only an evidence of the REMOTE body when the transaction itself verified it:
    # "body-readback" in verified_steps means the stored body matched the pre-create snapshot
    # (line endings / trailing whitespace normalised). Otherwise (dedupe hit, early failure) the
    # local text was never compared with the remote body, so no hash is claimed (None).
    body_sha256: str | None = None
    if "body-readback" in result.verified_steps and effective_body is not None:
        body_sha256 = hashlib.sha256(effective_body.encode("utf-8")).hexdigest()
    if node_id is None and result.status in {"success", "dedupe"}:
        # No fresh authoritative readback of the issue: not a verified success.
        return _dataclass_replace(
            result,
            status="partial_failure",
            failure_stage="node-id-readback",
            failure_message="authoritative node_id readback failed; the issue is not verified",
            pending_steps=[*result.pending_steps, "node-id-readback"],
            body_sha256=body_sha256,
        )
    return _dataclass_replace(result, node_id=node_id, body_sha256=body_sha256)


def _reconcile_transaction_engine(
    *,
    repo: str,
    issue_number: int,
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    gh_bin: str,
    blocking_issue_numbers: list[int | str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TransactionResult:
    """Recover a partial_failure caused by label-readback mismatch.

    Applies labels via script and re-runs readback. Does NOT perform raw
    gh issue edit / gh api mutations directly. Only script-mediated recovery.
    """
    blocking_issue_numbers = blocking_issue_numbers or []
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    completed: list[str] = []
    applied_steps: list[str] = []
    verified_steps: list[str] = []
    pending_steps: list[str] = []
    failed_readbacks: list[dict] = []

    # #1946 Owner P0-2: normalize BEFORE validate, and both BEFORE any GitHub operation
    # (including the label re-apply below). The previous implementation validated
    # duplicate/self-ref/cross-direction shape via _validate_dependency_directions but
    # never rejected non-positive integers/booleans/cross-repo URLs here (unlike
    # run_transaction, which normalizes first) -- an input like `--blocking 0` would
    # reach label mutation before failing at node-id resolution for issue #0.
    try:
        normalized_dependency_issue_numbers = _normalize_dependency_numbers(
            list(dependency_issue_numbers), label="--blocked-by"
        )
        normalized_blocking_issue_numbers = _normalize_dependency_numbers(
            list(blocking_issue_numbers), label="--blocking"
        )
        _validate_dependency_directions(
            normalized_dependency_issue_numbers,
            normalized_blocking_issue_numbers,
            self_issue_number=issue_number,
        )
    except TransactionError as exc:
        return TransactionResult(
            status="failure",
            issue_number=issue_number,
            issue_url=issue_url,
            completed_steps=completed,
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    dependency_issue_numbers = normalized_dependency_issue_numbers
    blocking_issue_numbers = normalized_blocking_issue_numbers

    # Re-apply labels (idempotent)
    if labels:
        try:
            _issue_apply_labels(repo, issue_number, labels, gh_bin)
            completed.append("label")
            applied_steps.append("label")
        except TransactionError as exc:
            return TransactionResult(
                status="partial_failure",
                issue_number=issue_number,
                issue_url=issue_url,
                completed_steps=completed,
                failure_stage=exc.stage,
                failure_message=exc.message,
                applied_steps=applied_steps,
                verified_steps=verified_steps,
                pending_steps=pending_steps,
            )

        # Read back labels; use _readback_labels for bool check, then _readback_labels_with_result for diagnostics
        labels_verified = _readback_labels(repo, issue_number, labels, gh_bin, sleep_fn=sleep_fn)
        if not labels_verified:
            label_rb_result = _readback_labels_with_result(repo, issue_number, labels, gh_bin, sleep_fn=sleep_fn)
            return TransactionResult(
                status="partial_failure",
                issue_number=issue_number,
                issue_url=issue_url,
                completed_steps=completed,
                failure_stage="label-readback",
                failure_message="label read-back mismatch after reconcile",
                failed_readbacks=[{
                    "stage": "label-readback",
                    "expected_labels": label_rb_result.expected_labels,
                    "actual_labels": label_rb_result.actual_labels,
                    "attempts": label_rb_result.attempts,
                    "retry_delays": label_rb_result.retry_delays,
                    "error_kind": label_rb_result.error_kind,
                    "message": "label read-back mismatch after reconcile",
                }],
                applied_steps=applied_steps,
                verified_steps=verified_steps,
                pending_steps=["label-readback"],
            )
        completed.append("label-readback")
        verified_steps.append("label-readback")

    # Re-register sub-issue if requested
    parent_verified: bool | None = None
    if parent_issue_number:
        try:
            _, child_db_id = _issue_graphql_ids(repo, issue_number, gh_bin)
            _issue_register_sub_issue_idempotent(
                repo, parent_issue_number, child_db_id, issue_number, gh_bin
            )
            completed.append("sub_issue")
            applied_steps.append("sub_issue")
            parent_verified = _readback_parent_issue_with_retry(
                repo, issue_number, parent_issue_number, gh_bin, sleep_fn=sleep_fn
            )
            if parent_verified:
                completed.append("sub-issue-readback")
                verified_steps.append("sub-issue-readback")
            else:
                # Blocker B: parent readback False must be recorded in failed_readbacks.
                failed_readbacks.append({
                    "stage": "sub-issue-readback",
                    "expected_labels": [],
                    "actual_labels": [],
                    "attempts": 1,
                    "retry_delays": list(_PARENT_READBACK_RETRY_DELAYS),
                    "error_kind": "missing_expected_labels",
                    "message": "parent read-back mismatch in reconcile",
                })
                pending_steps.append("sub-issue-readback")
        except TransactionError as exc:
            return TransactionResult(
                status="partial_failure",
                issue_number=issue_number,
                issue_url=issue_url,
                completed_steps=completed,
                failure_stage=exc.stage,
                failure_message=exc.message,
                parent_verified=parent_verified,
                applied_steps=applied_steps,
                verified_steps=verified_steps,
                pending_steps=pending_steps,
            )

    # Re-register dependencies if requested. #1946 Owner P0-1: uses the same shared
    # apply/readback helper as create/dedupe -- computes the diff against the current
    # state and mutates only what's missing (idempotent, and avoids relying on 422
    # swallowing to detect "already registered").
    dependency_verified: bool | None = None
    if dependency_issue_numbers:
        try:
            dep_apply = _apply_relationship_direction(
                repo,
                issue_number,
                list(dependency_issue_numbers),
                "blocked_by",
                gh_bin,
                sleep_fn=sleep_fn,
            )
            if dep_apply.mutated_numbers:
                applied_steps.append("dependency")

            dependency_verified = dep_apply.verified
            if dependency_verified:
                completed.append("dependency-readback")
                verified_steps.append("dependency-readback")
            else:
                failed_readback = dep_apply.failed_readback or {
                    "stage": "dependency-readback",
                    "direction": "blocked_by",
                    "expected_issue_numbers": list(dependency_issue_numbers),
                    "actual_issue_numbers": "actual_state_unavailable",
                    "expected_labels": [],
                    "actual_labels": [],
                    "attempts": 2,
                    "retry_delays": [2.0],
                    "error_kind": "missing_expected_labels",
                    "message": "dependency (blocked-by) read-back mismatch after reconcile",
                }
                failed_readbacks.append(failed_readback)
                pending_steps.append("dependency-readback")
        except TransactionError as exc:
            return TransactionResult(
                status="partial_failure",
                issue_number=issue_number,
                issue_url=issue_url,
                completed_steps=completed,
                failure_stage=exc.stage,
                failure_message=exc.message,
                dependency_verified=dependency_verified,
                applied_steps=applied_steps,
                verified_steps=verified_steps,
                pending_steps=pending_steps,
            )

    # Re-register blocking relationships if requested (mirrors the --blocked-by block
    # above, using the same shared apply/readback helper).
    blocking_verified: bool | None = None
    if blocking_issue_numbers:
        try:
            blk_apply = _apply_relationship_direction(
                repo,
                issue_number,
                list(blocking_issue_numbers),
                "blocking",
                gh_bin,
                sleep_fn=sleep_fn,
            )
            if blk_apply.mutated_numbers:
                applied_steps.append("blocking")

            blocking_verified = blk_apply.verified
            if blocking_verified:
                completed.append("blocking-readback")
                verified_steps.append("blocking-readback")
            else:
                failed_readback = blk_apply.failed_readback or {
                    "stage": "blocking-readback",
                    "direction": "blocking",
                    "expected_issue_numbers": list(blocking_issue_numbers),
                    "actual_issue_numbers": "actual_state_unavailable",
                    "expected_labels": [],
                    "actual_labels": [],
                    "attempts": 2,
                    "retry_delays": [2.0],
                    "error_kind": "missing_expected_labels",
                    "message": "blocking read-back mismatch after reconcile",
                }
                failed_readbacks.append(failed_readback)
                pending_steps.append("blocking-readback")
        except TransactionError as exc:
            return TransactionResult(
                status="partial_failure",
                issue_number=issue_number,
                issue_url=issue_url,
                completed_steps=completed,
                failure_stage=exc.stage,
                failure_message=exc.message,
                dependency_verified=dependency_verified,
                blocking_verified=blocking_verified,
                applied_steps=applied_steps,
                verified_steps=verified_steps,
                pending_steps=pending_steps,
            )

    if failed_readbacks:
        return TransactionResult(
            status="partial_failure",
            issue_number=issue_number,
            issue_url=issue_url,
            completed_steps=completed,
            failure_stage=failed_readbacks[0]["stage"],
            failure_message=failed_readbacks[0]["message"],
            parent_verified=parent_verified,
            dependency_verified=dependency_verified,
            blocking_verified=blocking_verified,
            failed_readbacks=failed_readbacks,
            applied_steps=applied_steps,
            verified_steps=verified_steps,
            pending_steps=pending_steps,
        )

    return TransactionResult(
        status="success",
        issue_number=issue_number,
        issue_url=issue_url,
        completed_steps=completed,
        parent_verified=parent_verified,
        dependency_verified=dependency_verified,
        blocking_verified=blocking_verified,
        applied_steps=applied_steps,
        verified_steps=verified_steps,
        pending_steps=pending_steps,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = _dispatch(args)
    except TransactionError as exc:
        # An uncaught TransactionError used to surface as a traceback with no JSON receipt.
        result = TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    sys.stdout.write(f"{json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True)}\n")
    if result.status in {"dedupe", "success"}:
        return 0
    if result.status == "partial_failure":
        return 2
    return 1


def _dispatch(args: argparse.Namespace) -> TransactionResult:
    if getattr(args, "subcommand", "create") == "reconcile":
        result = reconcile_transaction(
            repo=args.repo,
            issue_number=args.issue,
            labels=args.label,
            parent_issue_number=args.parent_issue,
            dependency_issue_numbers=args.dependency,
            blocking_issue_numbers=args.blocking,
            gh_bin=args.gh,
        )
    else:
        result = run_transaction(
            repo=args.repo,
            title=args.title,
            body=args.body,
            body_file=args.body_file,
            labels=args.label,
            issue_kind=args.issue_kind,
            label_profile=args.label_profile,
            parent_issue_number=args.parent_issue,
            dependency_issue_numbers=args.dependency,
            blocking_issue_numbers=args.blocking,
            gh_bin=args.gh,
            skip_internal_title_dedupe=args.skip_internal_title_dedupe,
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
