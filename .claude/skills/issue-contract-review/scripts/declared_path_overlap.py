#!/usr/bin/env python3
"""
declared_path_overlap.py

OPEN PR 一覧の完全性契約付き inventory 取得と、対象 Issue の Allowed Paths
との changed-file 名の単純な重なり（``declared_path_overlap``）の算出。

Issue #1680 の設計:

- これは実 Git merge 競合（3-way merge / hunk 競合 / rename・delete 競合）の
  証明ではない。changed-file 名の単純な重複を **advisory のみ** で記録する。
- 実 Git merge 競合の観測・blocking 判定は独立 producer（Issue #1792、
  ``PAIRWISE_MERGE_OBSERVATION_V1``）とその呼び出し元配線（Issue #1793）に
  分離済みであり、本モジュールの責務ではない。
- 単独では blocking にしない。呼び出し側（``run_contract_review_once.py``）
  はこの check の結果によって status を ``blocked`` / ``human_judgment`` に
  変えてはならない。

OPEN PR inventory の完全性契約（Issue #1680 In Scope）:

- ``state``: 常に ``open`` 固定（GraphQL ``states: OPEN`` 相当）
- ``base_ref``: 明示的にフィルタする場合のみ指定。フィルタしない場合も、
  各 PR エントリは自身の ``base_ref_name`` を明示的に保持する
- ``draft_policy``: ``include_drafts`` — draft PR も inventory に含める
  （各 PR エントリの ``is_draft`` で識別可能）
- fork PR は ``head_repository_owner`` / ``head_repository_name`` /
  ``head_ref_oid`` / ``is_cross_repository`` で識別する
- pagination は GraphQL cursor（``hasNextPage`` が ``false`` になるまで）で
  行い、``totalCount`` と ``fetched_count`` を cross-check する
- ``complete``: ``fetched_count == totalCount`` かつ ``has_next_page is False``
  かつエラーなしの場合のみ ``True``
- ``limit`` は safety cap（既定 200）。cap に到達しても ``has_next_page`` が
  ``True`` のままの場合は ``saturated: True`` とし、``complete`` を ``False``
  に倒す（fail-closed、全件性を証明できない場合は complete を騙らない）
- ``inventory_digest``: 収集した PR number / head_ref_oid の sha256 digest

Issue #1794 PR review (P0-2/P0-3/P1-1/P1-2/P1-3/P2) で以下を追加:

- tri-state ``disjoint`` 契約（P1-1）: 全件取得完了かつエラーなしの場合のみ
  ``disjoint: true|false``。一件でも取得失敗・saturated・budget 超過なら
  ``disjoint: null`` / ``decision: indeterminate|unavailable``。
- GraphQL レスポンスの ``errors`` フィールド検証（P1-1）。
- 全体 time budget（P0-3）— 呼び出し元の overall timeout を超過しないよう
  途中で打ち切り、残余を ``errors`` に記録する。
- ``exclude_pr_number``（P1-3）で呼び出し元自身の PR を inventory から除外可能。
- fetch 前後の head SHA 再検証（P1-3）— 不一致は overlap 判定から除外。
- GraphQL ``changedFiles`` カウントと実ファイル名件数の cross-check（P1-3/P2）。
- pagination の ``first`` を残り件数に応じて調整（P2）。
- ``scripts/agent-guards/changed_file_matcher.py`` の ``AllowedPathsMatcher``
  を再利用し、``*`` (1 階層) / ``**`` (再帰) の segment-based semantics を
  保持したまま matching する（P2）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_CHECK_ISSUE_OVERLAP_PY = (
    _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "check_issue_overlap.py"
)
_CHANGED_FILE_MATCHER_PY = _REPO_ROOT / "scripts" / "agent-guards" / "changed_file_matcher.py"

_DEFAULT_TIMEOUT = 30
_DEFAULT_LIMIT = 200
_PAGE_SIZE = 50
# P0-3: overall time budget for compute_declared_path_overlap(), kept below
# the 180s overall timeout used by callers (run_contract_review_once.py).
_OVERALL_TIME_BUDGET_SECONDS = 150

OPEN_PR_INVENTORY_SCHEMA = "OPEN_PR_INVENTORY_V1"
DECLARED_PATH_OVERLAP_SCHEMA = "declared_path_overlap/v1"

_OPEN_PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $first, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        baseRefName
        headRefName
        headRefOid
        isDraft
        isCrossRepository
        headRepositoryOwner { login }
        headRepository { name }
        url
        changedFiles
      }
    }
  }
}
"""


def _load_path_normalizer() -> Optional[Any]:
    """check_issue_overlap.py の Allowed Paths readback を正本として再利用する。"""
    import importlib.util

    if not _CHECK_ISSUE_OVERLAP_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "check_issue_overlap_for_declared_path_overlap", _CHECK_ISSUE_OVERLAP_PY
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Python 3.12 dataclass 実装は cls.__module__ を sys.modules から解決する
    # ため、exec_module 前に sys.modules へ登録しておかないと frozen
    # dataclass 定義時に AttributeError になる (dataclasses._is_type)。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_allowed_paths_matcher() -> Optional[Any]:
    """scripts/agent-guards/changed_file_matcher.py の AllowedPathsMatcher を
    正本として再利用する（P2: ``*``(1階層) / ``**``(再帰) の segment-based
    semantics を保持したまま Allowed Paths と changed-file 名を突き合わせる）。
    """
    import importlib.util

    if not _CHANGED_FILE_MATCHER_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "changed_file_matcher_for_declared_path_overlap", _CHANGED_FILE_MATCHER_PY
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return getattr(module, "AllowedPathsMatcher", None)


def _run_gh_graphql(
    query: str, variables: dict[str, Any], timeout: int = _DEFAULT_TIMEOUT
) -> tuple[Optional[dict], Optional[str]]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        cmd.extend(["-F", f"{key}={value}"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "gh_not_found"
    except OSError as exc:
        return None, f"subprocess_error: {exc}"
    if result.returncode != 0:
        return None, f"gh_error: {result.stderr.strip()[:200]}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"json_parse_error: {exc}"
    # P1-1: gh api graphql can return HTTP 200 with a populated top-level
    # "errors" array (e.g. secondary rate limiting, partial field errors).
    # Treat this the same as a transport failure -- it must not be silently
    # folded into a "complete" inventory.
    if isinstance(payload, dict) and payload.get("errors"):
        return None, f"graphql_errors: {json.dumps(payload['errors'], default=str)[:300]}"
    return payload, None


def _fetch_pr_head_sha(
    pr_number: int, repo: str, timeout: int = _DEFAULT_TIMEOUT
) -> tuple[Optional[str], Optional[str]]:
    """Re-read a PR's current head SHA (P1-3 fetch-before/after race guard)."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "gh_not_found"
    except OSError as exc:
        return None, f"subprocess_error: {exc}"
    if result.returncode != 0:
        return None, f"gh_error: {result.stderr.strip()[:200]}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"json_parse_error: {exc}"
    return payload.get("headRefOid"), None


def _fetch_ref_sha(repo: str, ref: str, timeout: int = _DEFAULT_TIMEOUT) -> Optional[str]:
    """Best-effort fetch of a branch's current tip SHA. Non-fatal on failure
    (P2: inventory_digest base_sha component; advisory only)."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{ref}", "-q", ".sha"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def fetch_open_pr_inventory(
    repo: str,
    base_ref: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    exclude_pr_number: Optional[int] = None,
) -> dict[str, Any]:
    """OPEN PR 一覧を完全性契約付きで取得する（``OPEN_PR_INVENTORY_V1``）。"""
    parts = repo.split("/")
    if len(parts) != 2:
        return {
            "schema": OPEN_PR_INVENTORY_SCHEMA,
            "repo": repo,
            "state": "open",
            "base_ref": base_ref,
            "base_sha": None,
            "draft_policy": "include_drafts",
            "prs": [],
            "fetched_count": 0,
            "totalCount": None,
            "has_next_page": None,
            "complete": False,
            "saturated": False,
            "limit": limit,
            "page_count": 0,
            "inventory_digest": None,
            "errors": ["invalid_repo_format"],
        }
    owner, name = parts

    prs: list[dict[str, Any]] = []
    seen_numbers: set[Any] = set()
    duplicate_pr_numbers: list[Any] = []
    cursor: Optional[str] = None
    total_count: Optional[int] = None
    total_count_values: set[int] = set()
    has_next_page = True
    errors: list[str] = []
    page_count = 0

    while has_next_page and len(prs) < limit:
        # P2: request only as many nodes as still needed instead of a fixed
        # page size, so limit=1 / limit=60 do not over-fetch.
        page_first = min(_PAGE_SIZE, limit - len(prs))
        if page_first <= 0:
            break
        variables: dict[str, Any] = {"owner": owner, "name": name, "first": page_first}
        if cursor:
            variables["cursor"] = cursor
        payload, err = _run_gh_graphql(_OPEN_PR_QUERY, variables)
        page_count += 1
        if err:
            errors.append(err)
            has_next_page = False
            break
        try:
            pr_connection = payload["data"]["repository"]["pullRequests"]  # type: ignore[index]
        except (KeyError, TypeError):
            errors.append("malformed_graphql_response")
            has_next_page = False
            break

        page_total = pr_connection.get("totalCount")
        if page_total is not None:
            total_count_values.add(page_total)
            total_count = page_total

        nodes = pr_connection.get("nodes")
        if nodes is None:
            errors.append("malformed_graphql_response: missing nodes")
            has_next_page = False
            break

        page_info = pr_connection.get("pageInfo") or {}
        next_has_next_page = bool(page_info.get("hasNextPage"))
        next_cursor = page_info.get("endCursor")

        if next_has_next_page and not nodes:
            errors.append("has_next_page_true_with_empty_nodes")
            has_next_page = False
            break
        if next_has_next_page and cursor is not None and next_cursor == cursor:
            errors.append("cursor_did_not_advance")
            has_next_page = False
            break

        for node in nodes:
            if not isinstance(node, dict):
                errors.append("malformed_pr_node")
                continue
            number = node.get("number")
            if number is None:
                errors.append("malformed_pr_node: missing number")
                continue
            if number in seen_numbers:
                duplicate_pr_numbers.append(number)
                continue
            seen_numbers.add(number)
            head_owner = (node.get("headRepositoryOwner") or {}).get("login")
            head_repo_name = (node.get("headRepository") or {}).get("name")
            prs.append(
                {
                    "number": number,
                    "title": node.get("title"),
                    "base_ref_name": node.get("baseRefName"),
                    "head_ref_name": node.get("headRefName"),
                    "head_ref_oid": node.get("headRefOid"),
                    "is_draft": node.get("isDraft"),
                    "is_cross_repository": node.get("isCrossRepository"),
                    "head_repository_owner": head_owner,
                    "head_repository_name": head_repo_name,
                    "url": node.get("url"),
                    "changed_files_count_graphql": node.get("changedFiles"),
                }
            )

        has_next_page = next_has_next_page
        cursor = next_cursor

    if duplicate_pr_numbers:
        errors.append(f"duplicate_pr_numbers: {sorted(set(duplicate_pr_numbers))}")
    if len(total_count_values) > 1:
        errors.append(f"total_count_drift: {sorted(total_count_values)}")

    # raw_count: count BEFORE base_ref/exclude_pr_number post-filtering, used
    # for the totalCount cross-check (base_ref/exclude filters intentionally
    # remove entries that legitimately existed in the OPEN PR set).
    raw_count = len(prs)

    if base_ref:
        prs = [pr for pr in prs if pr.get("base_ref_name") == base_ref]
    if exclude_pr_number is not None:
        prs = [pr for pr in prs if pr.get("number") != exclude_pr_number]

    saturated = has_next_page and raw_count >= limit
    fetched_count = len(prs)
    complete = (
        not errors
        and not saturated
        and not has_next_page
        and total_count is not None
        and (
            base_ref is not None
            or exclude_pr_number is not None
            or raw_count == total_count
        )
    )

    base_sha = _fetch_ref_sha(repo, base_ref) if base_ref else None

    # P2: inventory_digest now also folds in base_ref/base_sha and each PR's
    # draft/fork identity, not just (number, head_ref_oid).
    digest_source = json.dumps(
        {
            "base_ref": base_ref,
            "base_sha": base_sha,
            "prs": sorted(
                (
                    pr["number"],
                    pr.get("head_ref_oid"),
                    pr.get("is_draft"),
                    pr.get("is_cross_repository"),
                    pr.get("head_repository_owner"),
                    pr.get("head_repository_name"),
                    pr.get("base_ref_name"),
                )
                for pr in prs
                if pr.get("number") is not None
            ),
        },
        sort_keys=True,
        default=str,
    )
    inventory_digest = "sha256:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

    return {
        "schema": OPEN_PR_INVENTORY_SCHEMA,
        "repo": repo,
        "state": "open",
        "base_ref": base_ref,
        "base_sha": base_sha,
        "draft_policy": "include_drafts",
        "prs": prs,
        "fetched_count": fetched_count,
        "totalCount": total_count,
        "has_next_page": has_next_page,
        "complete": complete,
        "saturated": saturated,
        "limit": limit,
        "page_count": page_count,
        "inventory_digest": inventory_digest,
        "errors": errors,
    }


def fetch_pr_changed_files(
    pr_number: int, repo: str, timeout: int = _DEFAULT_TIMEOUT
) -> tuple[list[str], Optional[str]]:
    """``gh pr diff --name-only`` 相当。ファイル名のみを返す（overlap の証明ではない）。

    P1-3/P2: この件数だけでは完全性を証明しない（3000 件超のような極端な
    diff で ``gh pr diff --name-only`` が切り詰められても検出できない）。
    呼び出し元は GraphQL の ``changedFiles`` カウントとの cross-check
    （``pr_{N}_changed_files_count_mismatch``）を独立に行う。
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo, "--name-only"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], "timeout"
    except FileNotFoundError:
        return [], "gh_not_found"
    except OSError as exc:
        return [], f"subprocess_error: {exc}"
    if result.returncode != 0:
        return [], f"gh_error: {result.stderr.strip()[:200]}"
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return files, None


def _digest_files(files: list[str]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(sorted(files), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _unavailable_result(reason: str) -> dict[str, Any]:
    return {
        "schema": DECLARED_PATH_OVERLAP_SCHEMA,
        "advisory": True,
        "blocking": False,
        "decision": "unavailable",
        "disjoint": None,
        "overlapping_prs": [],
        "inventory": None,
        "errors": [reason],
        "note": (
            "changed-file 名の単純な重なりのみを証明する advisory check。"
            "実 Git merge 競合の証明ではなく、単独では blocking にしない。"
        ),
    }


def compute_declared_path_overlap(
    allowed_paths: list[str],
    repo: str,
    base_ref: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    exclude_pr_number: Optional[int] = None,
    time_budget_seconds: float = _OVERALL_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """``declared_path_overlap``（advisory のみ）を算出する。

    対象 Issue の Allowed Paths と OPEN PR の changed-file 名が
    ``disjoint``（重なりなし）であれば ``overlapping_prs`` は空になる。
    これは実 Git merge 競合の証明ではない。3-way merge・hunk 競合・
    rename/delete 競合は評価しない。単独では blocking にしない
    （advisory のみ、記録専用 — Issue #1792 / #1793 に分離済み）。

    P1-1 tri-state 契約: 全 PR の changed-file 取得が完了しエラーが
    ゼロの場合のみ ``disjoint: true|false``。一件でも失敗・saturated・
    time-budget 超過があれば ``disjoint: null`` / ``decision:
    indeterminate`` (部分観測あり) または ``unavailable`` (観測不能)。

    ``allowed_paths`` は装飾除去済みだが glob suffix（``/*`` / ``/**`` /
    trailing ``/``）を保持した raw entry を想定する（P2: 1 階層 glob と
    再帰 glob の区別を保つため）。
    """
    start_time = time.monotonic()

    matcher = _load_allowed_paths_matcher()
    if matcher is None:
        return _unavailable_result("path_matcher_unavailable")

    if not allowed_paths:
        return _unavailable_result("no_allowed_paths")

    inventory = fetch_open_pr_inventory(
        repo, base_ref=base_ref, limit=limit, exclude_pr_number=exclude_pr_number
    )

    overlapping_prs: list[dict[str, Any]] = []
    errors = list(inventory.get("errors", []))
    budget_exceeded = False
    all_prs = inventory.get("prs", []) or []

    for idx, pr in enumerate(all_prs):
        elapsed = time.monotonic() - start_time
        if elapsed >= time_budget_seconds:
            remaining = len(all_prs) - idx
            errors.append(
                f"declared_path_overlap_budget_exceeded: remaining_prs={remaining}"
            )
            budget_exceeded = True
            break

        pr_number = pr.get("number")
        if pr_number is None:
            continue

        head_before = pr.get("head_ref_oid")
        changed_files, err = fetch_pr_changed_files(pr_number, repo)
        if err:
            errors.append(f"pr_{pr_number}_changed_files_error: {err}")
            continue

        # P1-3: re-read the head SHA immediately after the changed-files
        # fetch. If the PR was force-pushed / updated concurrently, the
        # file list we just fetched may no longer describe the PR's
        # current state -- exclude it from the overlap decision (safe
        # side) rather than risk a stale false positive/negative.
        head_after, sha_err = _fetch_pr_head_sha(pr_number, repo)
        if sha_err:
            errors.append(f"pr_{pr_number}_head_sha_refetch_error: {sha_err}")
        elif head_before and head_after and head_before != head_after:
            errors.append(f"pr_{pr_number}_head_sha_changed_during_fetch")
            continue

        graphql_count = pr.get("changed_files_count_graphql")
        if isinstance(graphql_count, int) and graphql_count != len(changed_files):
            errors.append(
                f"pr_{pr_number}_changed_files_count_mismatch: "
                f"graphql={graphql_count} fetched={len(changed_files)}"
            )

        matched_files = [
            path for path in changed_files if matcher.is_file_allowed(path, allowed_paths)
        ]
        if matched_files:
            overlapping_prs.append(
                {
                    "pr_number": pr_number,
                    "url": pr.get("url"),
                    "head_ref_oid": pr.get("head_ref_oid"),
                    "is_draft": pr.get("is_draft"),
                    "is_cross_repository": pr.get("is_cross_repository"),
                    "matched_files": matched_files,
                    "changed_files_sha256": _digest_files(changed_files),
                }
            )

    complete_observation = (
        inventory.get("complete") is True and not errors and not budget_exceeded
    )

    if complete_observation:
        disjoint: Optional[bool] = len(overlapping_prs) == 0
        decision = "advisory_only"
    else:
        disjoint = None
        decision = "indeterminate" if (all_prs or errors) else "unavailable"

    inventory_digest_extended = "sha256:" + hashlib.sha256(
        json.dumps(
            {
                "base_ref": inventory.get("base_ref"),
                "base_sha": inventory.get("base_sha"),
                "prs": sorted(
                    (
                        pr.get("number"),
                        pr.get("head_ref_oid"),
                        pr.get("is_draft"),
                        pr.get("is_cross_repository"),
                        pr.get("head_repository_owner"),
                        pr.get("head_repository_name"),
                    )
                    for pr in all_prs
                    if pr.get("number") is not None
                ),
                "overlapping_pr_changed_files_digests": sorted(
                    (pr["pr_number"], pr["changed_files_sha256"])
                    for pr in overlapping_prs
                ),
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    return {
        "schema": DECLARED_PATH_OVERLAP_SCHEMA,
        "advisory": True,
        "blocking": False,
        "decision": decision,
        "disjoint": disjoint,
        "overlapping_prs": overlapping_prs,
        "inventory": inventory,
        "inventory_digest_extended": inventory_digest_extended,
        "errors": errors,
        "note": (
            "changed-file 名の単純な重なりのみを証明する advisory check。"
            "3-way merge・hunk 競合・rename/delete 競合は評価しない"
            "（実 Git merge 競合の判定は Issue #1792 の "
            "PAIRWISE_MERGE_OBSERVATION_V1 producer の責務）。"
            "単独では blocking にしない。disjoint は tri-state（true/"
            "false/null）— 観測が不完全な場合は null。"
        ),
    }


def compute_declared_path_overlap_for_issue(
    issue_number: int,
    repo: str,
    base_ref: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    exclude_pr_number: Optional[int] = None,
) -> dict[str, Any]:
    """Issue 本文から Allowed Paths を readback し ``declared_path_overlap`` を返す。"""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _unavailable_result("issue_body_fetch_timeout")
    except FileNotFoundError:
        return _unavailable_result("gh_not_found")
    except OSError as exc:
        return _unavailable_result(f"issue_body_fetch_error: {exc}")
    if result.returncode != 0:
        return _unavailable_result(f"issue_body_fetch_gh_error: {result.stderr.strip()[:200]}")
    try:
        body = json.loads(result.stdout).get("body", "")
    except json.JSONDecodeError as exc:
        return _unavailable_result(f"issue_body_json_parse_error: {exc}")

    normalizer = _load_path_normalizer()
    if normalizer is None:
        return _unavailable_result("path_normalizer_unavailable")

    # P2: use the decoration-stripped-but-glob-preserving entries (not
    # normalize_paths()/extract_allowed_paths(), which collapse "/*" and
    # "/**" down to the same bare directory and lose the 1-level vs
    # recursive distinction).
    allowed_path_entries = normalizer.extract_allowed_path_entries(body)
    return compute_declared_path_overlap(
        allowed_paths=allowed_path_entries,
        repo=repo,
        base_ref=base_ref,
        limit=limit,
        exclude_pr_number=exclude_pr_number,
    )
