#!/usr/bin/env python3
"""Build and emit IMPL_REVIEW_INTAKE_CAPSULE_V1 for impl-review-loop."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[3]
_DEFAULT_REPO = "squne121/agent-platform"
_DEFAULT_MAX_STDOUT_BYTES = 4096
_DEFAULT_SAMPLE_PATHS = 10
_SCHEMA_NAME = "IMPL_REVIEW_INTAKE_CAPSULE_V1"
_SCHEMA_VERSION = 1
_DEFAULT_ARTIFACT_DIR = _REPO_ROOT / "artifacts" / "impl-review-loop"
_TRIAGE_SCHEMA = "CONTRACT_BLOCKER_TRIAGE_V1"
_TRIAGE_PATH = _SCRIPT_DIR / "triage_contract_blockers.py"
# #1475: shared GitHub provenance trust policy (single source of truth).
_CRP_PATH = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "contract_review_result_parser.py"
_BASELINE_PREFLIGHT_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
)
_ALLOWED_PATHS_GATE_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts" / "allowed_paths_review_gate.py"
)
_IMPLEMENTATION_LANDED_EVIDENCE_PATH = _SCRIPT_DIR / "implementation_landed_evidence.py"
# #2713 AC1: single production import site for the pre-Step-1 data-plane
# route consumer (`resolve_pre_step1_data_plane_action()`).
_ROUTE_LOOP_VERDICT_V2_PATH = _SCRIPT_DIR / "route_loop_verdict_v2.py"
# #2713 In Scope: `product_spec_routing_action` is purely echoed as
# provenance metadata by `resolve_already_satisfied_early_exit_decision()`
# (it never affects the early_exit / dispatch_step1 boolean -- see that
# function's docstring in route_loop_verdict_v2.py). This evidence-based
# landing-disposition choke point (preparation.md "0-a-0") runs BEFORE
# preparation.md's separate "1-d. Product Spec Check" step, so no real
# `product_spec_preflight.routing_action` value exists yet at this call
# site. Inventing an arbitrary real-looking value (e.g. "continue") here
# would misrepresent a check that never ran, so an explicit, self-
# documenting "not yet evaluated" sentinel is used instead.
_PRE_STEP1_PRODUCT_SPEC_ROUTING_ACTION_NOT_YET_EVALUATED = "not_yet_evaluated_at_pre_step1_landing_disposition"
_FENCED_YAML_RE = re.compile(r"```ya?ml[ \t]*\n(.*?)```", re.DOTALL)
_CONTRACT_REVIEW_MARKER = "CONTRACT_REVIEW_RESULT_V1"
# #1950 AC6-AC8: comment-id resolution for --human-context-comment-url /
# --agent-report-comment-url. Origin is decided ONLY by which CLI flag the
# caller passed the URL to -- never inferred from comment body shape,
# author account, or author_association (a single account can post both
# human comments and machine-generated ones, e.g. create-issue transaction
# reports).
_ISSUE_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)$")
# PR #1973 (OWNER REQUEST_CHANGES, P1-3): closed allowlist of schema IDs a
# SubAgent's structured comment-body report may declare as its top-level
# fenced-block key. Verified (grep) against real usage in
# .claude/agents/*.md / .claude/skills/**/*.md -- each entry is a schema this
# repo's SubAgents actually produce as a PR/Issue comment body report, not a
# guessed name.
_ALLOWED_AGENT_REPORT_SCHEMA_IDS = frozenset(
    {
        "IMPLEMENT_RESULT_V1",
        "VC_ADJUDICATION_RESULT_V1",
        "TEST_VERDICT",
        "POST_MERGE_CLEANUP_REPORT_V1",
        "ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1",
        "CONTRACT_REVIEW_RESULT_V1",
    }
)


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_command(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _record_command(
    command_log: list[dict[str, Any]],
    name: str,
    argv: list[str],
    rc: int,
    stdout: str,
    stderr: str,
) -> None:
    command_log.append(
        {
            "name": name,
            "argv_sha256": _sha256(json.dumps(argv, ensure_ascii=False)),
            "exit_code": rc,
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
            "collected_at": _now_utc(),
        }
    )


def _safe_load_json(payload: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _slugify(text: str) -> str:
    normalized = text.lower()
    parts = re.split(r"[:：]", normalized, maxsplit=1)
    body = parts[1] if len(parts) == 2 else parts[0]
    body = re.sub(r"[^a-z0-9]+", "-", body.strip())
    while "--" in body:
        body = body.replace("--", "-")
    return body.strip("-")[:40] or "issue"


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot_load_module:{path}")
    module = importlib.util.module_from_spec(spec)
    # #2713 AC4: register in sys.modules BEFORE exec_module() (matches
    # route_loop_verdict_v2.py's own pattern, lines 696-698, and
    # implementation_landed_evidence.py::_load_route_loop_verdict_v2_module()).
    # route_loop_verdict_v2.py uses `from __future__ import annotations` with
    # `@dataclass(frozen=True)` classes; CPython's dataclasses internals
    # resolve stringified ClassVar/InitVar annotations via
    # `sys.modules.get(cls.__module__)`, which raises AttributeError during
    # class decoration if the module is not yet registered under its own
    # __name__ at that point.
    #
    # #2713 AC10 (PR #2741 review, P2): register-before-exec must not destroy
    # a same-named sys.modules entry that already existed BEFORE this loader
    # ran. `prior_module` snapshots whatever was there first; on exec_module()
    # failure, that original entry is restored (not merely pop()-ed) so only
    # the entry this loader itself inserted is ever removed.
    prior_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        if prior_module is not None:
            sys.modules[spec.name] = prior_module
        else:
            sys.modules.pop(spec.name, None)
        raise
    return module


def _parse_simple_yaml_block(block: str) -> dict[str, Any]:
    try:
        import yaml

        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except Exception:
        pass

    result: dict[str, Any] = {}
    lines = block.splitlines()
    current_key: str | None = None

    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        if indent == 0:
            current_key = None
            match = re.match(r"^(\S[^:]*?):\s*(.*)", stripped)
            if not match:
                continue
            key = match.group(1).strip()
            value = match.group(2).strip()
            if value:
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                result[key] = value
            else:
                result[key] = None
                current_key = key
            continue

        if current_key is None:
            continue
        submatch = re.match(r"^\s+(\S[^:]*?):\s*(.*)", stripped)
        if not submatch:
            continue
        subkey = submatch.group(1).strip()
        subvalue = submatch.group(2).strip()
        if (subvalue.startswith('"') and subvalue.endswith('"')) or (
            subvalue.startswith("'") and subvalue.endswith("'")
        ):
            subvalue = subvalue[1:-1]
        current = result.get(current_key)
        if not isinstance(current, dict):
            current = {}
            result[current_key] = current
        current[subkey] = subvalue or None

    return result


def _extract_yaml_blocks(body: str) -> list[str]:
    return [match.group(1) for match in _FENCED_YAML_RE.finditer(body)]


def _live_allowed_paths_hash(body: str) -> str | None:
    """Compute the reviewer-compatible hash from the live Issue body."""
    baseline = _load_module(_BASELINE_PREFLIGHT_PATH, "baseline_allowed_paths_for_intake")
    gate = _load_module(_ALLOWED_PATHS_GATE_PATH, "allowed_paths_gate_for_intake")
    paths = baseline.extract_allowed_paths(body)
    if not isinstance(paths, list) or not paths:
        return None
    canonicalized: list[str] = []
    for path in paths:
        normalized = gate.AllowedPathsMatcher.normalize_allowed_pattern(path)
        if normalized is None:
            return None
        canonicalized.append(normalized)
    if not canonicalized:
        return None
    return hashlib.sha256(
        json.dumps(sorted(set(canonicalized)), separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


_ISSUE_NUMBER_FROM_URL_RE = re.compile(r"/issues/(\d+)\Z")


def _issue_number_from_url(issue_url: str) -> int | None:
    m = _ISSUE_NUMBER_FROM_URL_RE.search(issue_url or "")
    return int(m.group(1)) if m else None


def _collect_issue_metadata(
    issue_number: int,
    repo: str,
    command_log: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    argv = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "title,state,labels,body,updatedAt",
    ]
    rc, stdout, stderr = _run_command(argv)
    _record_command(command_log, "issue_view", argv, rc, stdout, stderr)
    if rc != 0:
        return {"error": "gh_issue_view_failed", "stderr": stderr.strip()}, ["issue_view_failed"]

    payload = _safe_load_json(stdout)
    if payload is None:
        return {"error": "gh_issue_view_invalid_json", "stderr": stderr.strip()}, ["issue_view_invalid_json"]

    title = str(payload.get("title") or "")
    labels = payload.get("labels") or []
    label_names = [str(item.get("name") or "").lower() for item in labels if isinstance(item, dict)]
    body = str(payload.get("body") or "")
    updated_at = str(payload.get("updatedAt") or "")
    title_prefix_ok = title.startswith("実装:") or title.startswith("implement:")
    phase_label_ok = "phase/implementation" in label_names
    issue_state_open = str(payload.get("state") or "").lower() == "open"
    # #2084: label presence is presentation-only metadata and MUST NOT
    # participate in readiness authority. `phase_label_ok` is retained below
    # only as an observational field (`ready_tuple.phase_label_present`);
    # `ready_status` (consumed by `_next_action_route`) is derived solely
    # from non-label evidence (title prefix + native issue open state).
    ready_status = "pass" if all([title_prefix_ok, issue_state_open]) else "failed"

    return {
        "title": title,
        "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
        "body_sha256": _sha256(body),
        "body": body,
        "updated_at": updated_at,
        "ready_tuple": {
            "title_prefix_ok": title_prefix_ok,
            "phase_label_present": phase_label_ok,
            "issue_state_open": issue_state_open,
            "status": ready_status,
        },
    }, []


def _collect_issue_comments(
    issue_number: int,
    repo: str,
    command_log: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    argv = [
        "gh",
        "api",
        "--paginate",
        f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
        "--jq",
        ".[] | {id, html_url, created_at, updated_at, body, "
        "author: .user.login, author_id: .user.id, "
        "author_type: .user.type, author_association}",
    ]
    rc, stdout, stderr = _run_command(argv)
    _record_command(command_log, "issue_comments", argv, rc, stdout, stderr)
    if rc != 0:
        return [], {"invalid_json_lines_count": 0}, ["comments_fetch_error"]

    comments: list[dict[str, Any]] = []
    invalid_json_lines_count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            invalid_json_lines_count += 1
            continue
        if isinstance(loaded, dict):
            comments.append(loaded)
        else:
            invalid_json_lines_count += 1

    return comments, {"invalid_json_lines_count": invalid_json_lines_count}, []


def _validate_agent_report_schema(body: str) -> tuple["str | None", str, list[str]]:
    """PR #1973 (OWNER REQUEST_CHANGES, P1-3): validate that ``body`` carries
    EXACTLY ONE top-level fenced block whose parsed top-level dict key is a
    member of ``_ALLOWED_AGENT_REPORT_SCHEMA_IDS``.  It uses the local
    ``_extract_yaml_blocks()`` / ``_parse_simple_yaml_block()`` helpers only
    for agent-report schema validation.  Contract snapshots use the shared
    contract-review parser below, so this local parser cannot decode or
    authorize their fingerprint transport.

    Fail-closed (``validated_schema_id=None``, ``validation_status="fail_closed"``)
    when: zero blocks, more than one block, unparseable, or
    parsed-but-marker-not-in-allowlist. A marker matched only inside a
    quoted/nested string (not the actual top-level dict key) never counts --
    only the parsed top-level dict's own keys are consulted, never a bare
    regex search over raw text.

    Returns ``(validated_schema_id, validation_status, validation_errors)``.
    """
    blocks = _extract_yaml_blocks(body)
    if len(blocks) == 0:
        return None, "fail_closed", ["agent_report_no_structured_block"]
    if len(blocks) > 1:
        return None, "fail_closed", ["agent_report_multiple_blocks"]

    parsed = _parse_simple_yaml_block(blocks[0])
    if not isinstance(parsed, dict) or not parsed:
        return None, "fail_closed", ["agent_report_unparseable"]

    matched_ids = [key for key in parsed if key in _ALLOWED_AGENT_REPORT_SCHEMA_IDS]
    if not matched_ids:
        return None, "fail_closed", ["agent_report_schema_not_allowlisted"]

    return matched_ids[0], "ok", []


_ISSUE_OR_PR_COMMENT_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)#issuecomment-(\d+)$")


def _parse_issue_or_pr_comment_url(url: str) -> tuple[str, str, int, int] | None:
    """#2606: parse a github.com top-level conversation comment permalink
    shaped like ``.../issues/N#issuecomment-ID`` or
    ``.../pull/N#issuecomment-ID``.

    Returns ``(repo, kind, number, comment_id)`` where ``kind`` is
    ``"issues"`` or ``"pull"``, or ``None`` if the shape doesn't match. PR
    inline review comments (``#discussion_r<ID>``) and top-level PR review
    permalinks (``#pullrequestreview-<ID>``) intentionally never match this
    pattern (AC7) -- only top-level PR/Issue conversation issue comments are
    in scope.
    """
    match = _ISSUE_OR_PR_COMMENT_URL_RE.match(url)
    if match is None:
        return None
    return match.group(1), match.group(2), int(match.group(3)), int(match.group(4))


def _direct_lookup_comment(
    repo: str,
    comment_id: int,
    command_log: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """#2606 AC2/AC3: read-only comment-ID direct lookup, used ONLY when
    ``comment_id`` is absent from the already-fetched target-Issue
    ``comments_by_id`` map. Uses the GitHub Issues API comment-ID endpoint
    (``repos/{repo}/issues/comments/{comment_id}``), which resolves both
    Issue comments and PR conversation comments -- PR conversation issue
    comments are Issues-API comments under the hood."""
    argv = [
        "gh",
        "api",
        f"repos/{repo}/issues/comments/{comment_id}",
        "--jq",
        "{id, html_url, created_at, updated_at, body, "
        "author: .user.login, author_id: .user.id, "
        "author_type: .user.type, author_association}",
    ]
    rc, stdout, stderr = _run_command(argv)
    _record_command(command_log, "issue_comment_direct_lookup", argv, rc, stdout, stderr)
    if rc != 0:
        return None
    return _safe_load_json(stdout)


def _pr_closes_target_issue(
    repo: str,
    pr_number: int,
    issue_number: int,
    command_log: list[dict[str, Any]],
) -> bool:
    """#2606: read-only verification that ``pr_number`` references
    ``issue_number`` as a closing issue, via ``closingIssuesReferences``
    (``gh pr view --json closingIssuesReferences`` equivalent). A PR
    conversation comment on an unrelated PR (one that does NOT list the
    target Issue as a closing issue) must never be accepted (AC4)."""
    argv = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "closingIssuesReferences",
    ]
    rc, stdout, stderr = _run_command(argv)
    _record_command(command_log, "pr_view_closing_issues", argv, rc, stdout, stderr)
    if rc != 0:
        return False
    payload = _safe_load_json(stdout)
    if payload is None:
        return False
    refs = payload.get("closingIssuesReferences")
    if not isinstance(refs, list):
        return False
    expected_url = f"https://github.com/{repo}/issues/{issue_number}"
    return any(
        isinstance(ref, dict) and ref.get("number") == issue_number and ref.get("url") == expected_url for ref in refs
    )


def _resolve_context_comments(
    urls: list[str],
    lane: str,
    comments_by_id: dict[int, dict[str, Any]],
    issue_number: int,
    repo: str,
    command_log: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """#1950 AC6/AC7/AC8: resolve --human-context-comment-url /
    --agent-report-comment-url values against the already-fetched comments
    list (no new fetch path is added -- this only additively binds comment
    IDs the caller explicitly designated into ``comments_by_id``, which was
    built from the single existing all-pages ``_collect_issue_comments()``
    call).

    ``lane`` is either "human_supplied" or "agent_generated" -- it is never
    inferred from ``comment`` contents, only from which CLI flag the caller
    used to pass ``urls``.

    #2606 AC2-AC7: when ``comment_id`` is absent from ``comments_by_id``
    (i.e. it does not belong to the target Issue's own comments), a
    read-only comment-ID direct lookup (``_direct_lookup_comment()``) is
    attempted instead of immediately failing. The lookup result is
    exact-bound to the input ``url`` via ``html_url`` equality, and then
    additionally bound by shape:

    - ``/issues/N#issuecomment-ID``: ``N`` must equal ``issue_number``.
    - ``/pull/N#issuecomment-ID``: PR ``N`` must reference ``issue_number``
      as a closing issue (``_pr_closes_target_issue()``).

    Any URL shape other than these two (e.g. PR inline review comments,
    top-level PR review permalinks) is never routed through direct lookup
    at all -- it fails the initial parse.

    Returns ``(resolved_entries, errors)``. Any resolution failure
    (unparseable URL, comment not found, html_url readback mismatch,
    repo mismatch, issue-number mismatch, PR-not-closing-target-issue, or --
    for the agent_generated lane only -- a missing structured report
    marker) is fail-closed: the offending URL is dropped from
    ``resolved_entries`` and an error string is appended instead.
    """
    resolved: list[dict[str, Any]] = []
    errors: list[str] = []
    for url in urls:
        match = _ISSUE_COMMENT_ID_RE.search(url)
        if match is None:
            errors.append(f"{lane}_comment_url_unparseable:{url}")
            continue
        comment_id = int(match.group(1))
        comment = comments_by_id.get(comment_id)
        if comment is None:
            # #2606: map miss -> read-only comment-ID direct lookup,
            # restricted to top-level PR/Issue conversation comments and
            # exact-bound to the target Issue.
            parsed_url = _parse_issue_or_pr_comment_url(url)
            if parsed_url is None:
                errors.append(f"{lane}_comment_not_found:{comment_id}")
                continue
            url_repo, url_kind, url_number, _url_comment_id = parsed_url
            if url_repo != repo:
                errors.append(f"{lane}_comment_repo_mismatch:{comment_id}")
                continue
            looked_up = _direct_lookup_comment(repo, comment_id, command_log)
            if looked_up is None:
                errors.append(f"{lane}_comment_not_found:{comment_id}")
                continue
            if str(looked_up.get("html_url") or "") != url:
                errors.append(f"{lane}_comment_url_html_url_mismatch:{comment_id}")
                continue
            if url_kind == "issues":
                if url_number != issue_number:
                    errors.append(f"{lane}_comment_issue_number_mismatch:{comment_id}")
                    continue
            elif not _pr_closes_target_issue(repo, url_number, issue_number, command_log):
                errors.append(f"{lane}_comment_pr_not_closing_target_issue:{comment_id}")
                continue
            comment = looked_up
        html_url = str(comment.get("html_url") or "")
        if html_url != url:
            errors.append(f"{lane}_comment_url_html_url_mismatch:{comment_id}")
            continue
        body = str(comment.get("body") or "")
        entry: dict[str, Any] = {
            "comment_id": comment_id,
            "url": url,
            "author": comment.get("author"),
            "author_id": comment.get("author_id"),
            "author_type": comment.get("author_type"),
            "author_association": comment.get("author_association"),
            "updated_at": comment.get("updated_at"),
            "body_sha256": _sha256(body),
            "body": body,
        }
        if lane == "agent_generated":
            validated_schema_id, validation_status, validation_errors = _validate_agent_report_schema(body)
            entry["validated_schema_id"] = validated_schema_id
            entry["validation_status"] = validation_status
            entry["validation_errors"] = validation_errors
            if validation_status != "ok":
                errors.extend(f"{err}:{comment_id}" for err in validation_errors)
                continue
        resolved.append(entry)
    return resolved, errors


def _parse_contract_results(
    comments: list[dict[str, Any]],
    expected_issue_url: str,
    issue_number: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Consume the shared parser; retain its invalid-block telemetry.

    ``issue_number`` remains in this local API for compatibility with existing
    callers.  Fingerprint source binding is derived by the shared parser from
    ``expected_issue_url`` so this consumer never reimplements decoding or
    readiness validation.
    """
    del issue_number
    module = _load_module(_CRP_PATH, "contract_review_result_parser_for_intake_capsule")
    return module.parse_contract_review_results_with_telemetry(comments, expected_issue_url)


def _find_latest_result(
    results: list[dict[str, Any]],
    *,
    trusted_only: bool = False,
) -> dict[str, Any] | None:
    """#1475 fix_delta P1 item 1: trusted_only=True filters to authoritative
    (trusted-author) candidates BEFORE selecting the latest entry, so an
    untrusted comment can never pre-empt a trusted go/blocked snapshot.
    Every caller that decides go/blocked precedence must pass
    trusted_only=True.
    """
    candidates = [item for item in results if item.get("is_trusted_author") is True] if trusted_only else results
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (str(item.get("created_at") or ""), int(item.get("comment_id") or 0)),
        reverse=True,
    )[0]


def _find_latest_go(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    # #1537: every consumer of an intake capsule requires a fully bound GO.
    go_results = [
        item
        for item in results
        if item.get("status") == "go"
        and item.get("is_trusted_author") is True
        and item.get("is_fingerprint_ready") is True
    ]
    return _find_latest_result(go_results)


def _build_triage(payload: dict[str, Any]) -> dict[str, Any]:
    triage_module = _load_module(_TRIAGE_PATH, "triage_contract_blockers")
    return triage_module.build_triage_result(payload)


def _top_blocked_categories(triage_result: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    for item in triage_result.get("per_ac") or []:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if isinstance(category, str) and category not in categories:
            categories.append(category)
    return categories


def _normalize_contract_snapshot_from_ensure(
    ensure_payload: dict[str, Any],
) -> dict[str, Any]:
    upstream_status = str(ensure_payload.get("status") or "")
    triage_result = (
        _build_triage(ensure_payload)
        if upstream_status in {"blocked_needs_refinement", "dry_run_would_post"}
        else {"schema": _TRIAGE_SCHEMA, "status": "not_run"}
    )
    top_categories = _top_blocked_categories(triage_result)

    normalized_status = "runtime_error"
    if upstream_status == "ok":
        normalized_status = "go"
    elif upstream_status in {"blocked_needs_refinement", "dry_run_would_post"}:
        normalized_status = "missing_go"
    elif upstream_status == "human_judgment":
        normalized_status = "human_judgment"
    elif upstream_status == "stale_or_conflicting_snapshot":
        normalized_status = "stale"

    return {
        "normalized_status": normalized_status,
        "upstream_schema": str(ensure_payload.get("schema") or "none"),
        "upstream_status": upstream_status or None,
        "source": str(ensure_payload.get("source") or "ensure_contract_snapshot_result"),
        "contract_snapshot_url": ensure_payload.get("contract_snapshot_url"),
        "top_blocked_categories": top_categories,
        "contract_blocker_triage": triage_result,
    }


def _normalize_contract_snapshot_live(
    issue_url: str,
    issue_body: str,
    issue_body_sha256: str,
    issue_updated_at: str,
    comments: list[dict[str, Any]],
    parse_warning_counts: dict[str, int],
) -> tuple[dict[str, Any], bool]:
    parsed_results, parser_counts = _parse_contract_results(comments, issue_url, _issue_number_from_url(issue_url))
    parse_warning_counts.update(parser_counts)

    # #1475 fix_delta P1 item 1: trust filtering before precedence -- an
    # untrusted comment must never pre-empt a trusted go via the
    # "latest blocked wins" branch below.
    #
    # #3012: a trusted ``go`` that is not fingerprint-ready is never adoptable
    # and must not become "latest" ahead of an earlier trusted blocked (same
    # ``adoptable`` semantics as verify_snapshot_authority_postcondition).
    adoptable = [
        item
        for item in parsed_results
        if item.get("status") != "go" or item.get("is_fingerprint_ready") is True
    ]
    latest = _find_latest_result(adoptable, trusted_only=True)
    latest_go = _find_latest_go(parsed_results)
    # #1537: a trusted go lacking a well-formed source-bound
    # expected_contract_fingerprint must never be routed as the
    # loop-consumable latest go -- treat it as if no go were found so the
    # caller re-materializes instead of adopting a fingerprint-incomplete
    # snapshot.
    if latest_go is not None and not latest_go.get("is_fingerprint_ready"):
        latest_go = None
    evidence_complete = not any(parse_warning_counts.values())

    # #1475 fix_delta P2 item 4: incomplete comment evidence (malformed
    # NDJSON lines, ambiguous duplicate contract blocks, invalid contract
    # blocks) must never be silently treated as "no conflicting evidence" --
    # a "go" adoption based on a partial comment fetch is fail-open. When
    # evidence is incomplete, do not report normalized_status: "go" even if
    # a trusted go candidate was found in the partial data; route to
    # missing_go so the caller re-fetches / escalates instead of proceeding.
    if not evidence_complete and latest_go and not (latest and latest.get("status") == "blocked"):
        return {
            "normalized_status": "missing_go",
            "upstream_schema": _CONTRACT_REVIEW_MARKER,
            "upstream_status": "go",
            "source": "incomplete_evidence",
            "contract_snapshot_url": latest_go.get("html_url"),
            "top_blocked_categories": [],
            "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
        }, evidence_complete

    if latest and latest.get("status") == "blocked":
        return {
            "normalized_status": "latest_blocked",
            "upstream_schema": _CONTRACT_REVIEW_MARKER,
            "upstream_status": "blocked",
            "source": "latest_blocked",
            "contract_snapshot_url": latest.get("html_url"),
            "top_blocked_categories": [],
            "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
        }, evidence_complete

    if latest_go:
        inner = latest_go.get("inner") or {}
        fingerprint = inner.get("expected_contract_fingerprint")
        live_paths_hash = _live_allowed_paths_hash(issue_body)
        if (
            not isinstance(fingerprint, dict)
            or live_paths_hash is None
            or fingerprint.get("allowed_paths_normalized_sha256") != live_paths_hash
        ):
            return {
                "normalized_status": "missing_go",
                "upstream_schema": _CONTRACT_REVIEW_MARKER,
                "upstream_status": "go",
                "source": "fingerprint_live_allowed_paths_mismatch",
                "contract_snapshot_url": latest_go.get("html_url"),
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
            }, evidence_complete
        body_sha256 = inner.get("body_sha256")
        generated_at = str(inner.get("generated_at") or "")
        if body_sha256 and body_sha256 != issue_body_sha256:
            return {
                "normalized_status": "stale",
                "upstream_schema": _CONTRACT_REVIEW_MARKER,
                "upstream_status": "go",
                "source": "existing_go",
                "contract_snapshot_url": latest_go.get("html_url"),
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
            }, evidence_complete
        if not body_sha256 and issue_updated_at and generated_at and generated_at < issue_updated_at:
            return {
                "normalized_status": "stale",
                "upstream_schema": _CONTRACT_REVIEW_MARKER,
                "upstream_status": "go",
                "source": "existing_go",
                "contract_snapshot_url": latest_go.get("html_url"),
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
            }, evidence_complete
        return {
            "normalized_status": "go",
            "upstream_schema": _CONTRACT_REVIEW_MARKER,
            "upstream_status": "go",
            "source": "existing_go",
            "contract_snapshot_url": latest_go.get("html_url"),
            "top_blocked_categories": [],
            "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
        }, evidence_complete

    return {
        "normalized_status": "missing_go",
        "upstream_schema": "none",
        "upstream_status": None,
        "source": "none",
        "contract_snapshot_url": None,
        "top_blocked_categories": [],
        "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
    }, evidence_complete


def _collect_repo_state(command_log: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []

    head_argv = ["git", "rev-parse", "HEAD"]
    head_rc, head_stdout, head_stderr = _run_command(head_argv)
    _record_command(command_log, "git_head", head_argv, head_rc, head_stdout, head_stderr)

    branch_argv = ["git", "branch", "--show-current"]
    branch_rc, branch_stdout, branch_stderr = _run_command(branch_argv)
    _record_command(command_log, "git_branch", branch_argv, branch_rc, branch_stdout, branch_stderr)

    status_argv = ["git", "status", "--short", "--porcelain=v1"]
    status_rc, status_stdout, status_stderr = _run_command(status_argv)
    _record_command(command_log, "git_status", status_argv, status_rc, status_stdout, status_stderr)

    if status_rc != 0:
        errors.append("git_status_failed")
        return {
            "worktree_status": "unavailable",
            "head_sha": head_stdout.strip() if head_rc == 0 else "",
            "current_branch": branch_stdout.strip() if branch_rc == 0 else "",
            "dirty": False,
            "dirty_paths_summary": {"count": 0, "sample_paths": [], "truncated": False},
        }, errors

    dirty_paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.strip():
            continue
        dirty_paths.append(line[3:].strip())

    sample_paths = sorted(dict.fromkeys(dirty_paths))
    return {
        "worktree_status": "dirty" if dirty_paths else "clean",
        "head_sha": head_stdout.strip() if head_rc == 0 else "",
        "current_branch": branch_stdout.strip() if branch_rc == 0 else "",
        "dirty": bool(dirty_paths),
        "dirty_paths_summary": {
            "count": len(dirty_paths),
            "sample_paths": sample_paths[:_DEFAULT_SAMPLE_PATHS],
            "truncated": len(sample_paths) > _DEFAULT_SAMPLE_PATHS,
        },
    }, errors


def _next_action_route(
    issue_ready_status: str,
    contract_snapshot: dict[str, Any],
) -> str:
    if issue_ready_status != "pass":
        return "request_readiness_check"

    normalized_status = contract_snapshot.get("normalized_status")
    if normalized_status in ("go", "missing_go", "stale", "runtime_error"):
        return "proceed_to_step_1"
    if normalized_status == "latest_blocked":
        return "run_contract_blocker_triage"
    return "human_review_required"


def _collect_implementation_landed_evidence(
    *,
    issue_number: int,
    repo: str,
    issue_body: str,
    command_log: list[dict[str, Any]],
    next_action_route: str | None = None,
    base_ac_verification_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect bounded pre-Step-1 candidate evidence through the strict producer.

    #2713: this is a single linear pipeline with no branch that re-derives
    disposition a second time from raw evidence (AC5):

    1. Evidence acquisition + freshness verification
       (`resolve_landing_disposition_with_freshness_rebind()`).
    2. Already-satisfied Disposition Precedence composition
       (`implementation_landed_evidence.py::apply_already_satisfied_precedence()`,
       AC3) using production-derived `pr_exists` / `base_ac_satisfied`
       inputs (see below) -- never a test-injected boolean, and never a
       fixed `True`/`False` fallback.
    3. Exactly one call into
       `route_loop_verdict_v2.py::resolve_pre_step1_data_plane_action()`
       against the FINAL (post-composition) disposition (AC1/AC2) -- the
       naive inline branch on the disposition name that used to live here
       is gone; the production route module owns that mapping exclusively.
    4. Non-destructive projection onto the existing `pre_step1_data_plane`
       capsule shape (`start_data_plane` / `action` -- unchanged key set,
       AC6).

    `pr_exists` (AC3): NOT "a PR existed at some point in history" -- it is
    "is the current landing-disposition candidate itself a target that a
    resume/conflict determination could act on", derived from the SAME
    candidate `resolve_landing_disposition_with_freshness_rebind()` already
    resolved via
    `implementation_landed_evidence.py::derive_pr_exists_from_landing_candidate()`
    (open/draft/merged -> True, closed-unmerged/no-candidate -> False; see
    that function's docstring for the closed-unmerged semantics).

    `base_ac_satisfied` (AC3): derived from an independent
    Verification-Commands evaluation of current main
    (`base_ac_verification_result`, a TEST_VERDICT_MACHINE/v2-shaped
    payload) via
    `implementation_landed_evidence.py::derive_base_ac_satisfied_from_verification_result()`
    (#2713 AC9: that function reuses `adjudicate_vc_result.py::
    adapt_test_verdict_to_current_vc_result()`'s existing validation rather
    than a second, weaker one), cross-checked against the SAME live
    `main_head_sha` this evidence collection step already fetched
    (`collect_candidate_inputs()`'s AC9 freshness reference) -- not a
    caller-asserted boolean.

    #2713 AC8 (PR #2741 review, P1-1): `build_intake_capsule.py` now wires a
    real production source for `base_ac_verification_result` -- the
    `--base-ac-verification-result-file <path>` CLI flag / the public
    `build_intake_capsule()` function's `base_ac_verification_result`
    parameter (see `main()` and `build_capsule_argv()` below; produced per
    preparation.md "0-a-1"'s current-main test-runner evaluation). When the
    caller supplies nothing (flag/parameter omitted, file missing, or file
    unparseable), `base_ac_verification_result` stays `None` and the
    derivation is undeterminable (`None`). Per #2713 In Scope, an
    undeterminable `base_ac_satisfied` is never fabricated as a fixed
    `True`/`False` -- `apply_already_satisfied_precedence()` is skipped
    entirely in that case and the freshness-rebound landing disposition
    passes through unchanged.
    """
    module = _load_module(
        _IMPLEMENTATION_LANDED_EVIDENCE_PATH,
        "implementation_landed_evidence",
    )
    route_module = _load_module(
        _ROUTE_LOOP_VERDICT_V2_PATH,
        "route_loop_verdict_v2",
    )

    def recorded_run(argv: list[str]) -> tuple[int, str, str]:
        rc, stdout, stderr = _run_command(argv)
        _record_command(
            command_log,
            "implementation_landed_candidate_discovery",
            argv,
            rc,
            stdout,
            stderr,
        )
        return rc, stdout, stderr

    # #2699 AC9: `resolve_landing_disposition_with_freshness_rebind()` is the
    # canonical collect -> verify-freshness-immediately-before-finalizing ->
    # bounded-retry-once -> derive entry point. It supersedes a bare
    # `collect_candidate_inputs()` + `derive_landing_disposition()` chain,
    # which never re-verified collection-time identity before finalizing.
    evidence = module.resolve_landing_disposition_with_freshness_rebind(
        repo=repo,
        issue_number=issue_number,
        current_scope=issue_body,
        run_command=recorded_run,
    )

    # #2713 AC3: Disposition Precedence step 2. See docstring above for the
    # `pr_exists` / `base_ac_satisfied` production-source semantics.
    landing_disposition = evidence["landing_disposition"]
    base_ac_satisfied = module.derive_base_ac_satisfied_from_verification_result(
        base_ac_verification_result,
        live_main_sha=evidence.get("main_head_sha"),
    )
    if base_ac_satisfied is None:
        final_disposition = landing_disposition
    else:
        pr_exists = module.derive_pr_exists_from_landing_candidate(landing_disposition.get("candidate"))
        final_disposition = module.apply_already_satisfied_precedence(
            landing_disposition,
            next_action_route=next_action_route or "unknown",
            product_spec_routing_action=_PRE_STEP1_PRODUCT_SPEC_ROUTING_ACTION_NOT_YET_EVALUATED,
            pr_exists=pr_exists,
            base_ac_satisfied=base_ac_satisfied,
        )
    evidence["landing_disposition"] = final_disposition

    # #2713 AC1/AC2/AC5: exactly one call into the production route module's
    # `resolve_pre_step1_data_plane_action()`, against the FINAL disposition.
    # This is the production control-plane projection consumed before any
    # worker/worktree/new-PR invocation.  It is deliberately explicit rather
    # than leaving callers to infer suppression from a prose disposition.
    data_plane = route_module.resolve_pre_step1_data_plane_action(final_disposition)
    # #2713 AC6: the existing capsule key set (`start_data_plane` / `action`)
    # is unchanged.
    evidence["pre_step1_data_plane"] = {
        "start_data_plane": data_plane["start_data_plane"],
        "action": data_plane["action"],
    }
    return evidence


def build_intake_capsule(
    issue_number: int,
    repo: str = _DEFAULT_REPO,
    ensure_contract_snapshot_result: str | None = None,
    human_context_comment_urls: list[str] | None = None,
    agent_report_comment_urls: list[str] | None = None,
    include_implementation_landed_evidence: bool = False,
    base_ac_verification_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    # #1869 fix_delta P0-4: `errors` is split into `fatal_errors` (blocks
    # intake / forces exit 1 — reserved for live Issue not accessible,
    # target identity unresolvable, or local worktree state unreadable) and
    # `warnings` (comment/snapshot/body-fingerprint fetch failures — these
    # never force a non-zero exit; they are advisory diagnostics only).
    command_log: list[dict[str, Any]] = []
    fatal_errors: list[str] = []
    warnings: list[str] = []

    # _collect_issue_metadata errors mean the live Issue itself could not be
    # read (gh_issue_view_failed / gh_issue_view_invalid_json) -- this is a
    # genuine "live Issue not accessible" fatal condition, not a semantic
    # planning/artifact issue.
    issue_meta, issue_errors = _collect_issue_metadata(issue_number, repo, command_log)
    fatal_errors.extend(issue_errors)

    # _collect_repo_state errors mean `git status` itself failed, so worktree
    # cleanliness/root-checkout cannot be determined at all -- fail closed
    # (this is the "root checkout / dirty worktree" safety-boundary case,
    # not a semantic planning/contract artifact case).
    repo_state, repo_errors = _collect_repo_state(command_log)
    fatal_errors.extend(repo_errors)

    if "error" in issue_meta:
        capsule = {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "generated_at": _now_utc(),
            "issue_number": issue_number,
            "repo": repo,
            "fatal_errors": fatal_errors,
            "errors": fatal_errors,  # backward-compat alias (deprecated)
            "warnings": warnings,
        }
        artifact = {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "generated_at": capsule["generated_at"],
            "commands": command_log,
            "input_error": issue_meta,
            "repo_state": repo_state,
        }
        return capsule, artifact, 1

    parse_warning_counts = {
        "invalid_json_lines_count": 0,
        "invalid_contract_blocks_count": 0,
        "ambiguous_contract_blocks_count": 0,
    }

    comments_for_digest: list[dict[str, Any]] = []
    if ensure_contract_snapshot_result:
        try:
            ensure_payload = json.loads(Path(ensure_contract_snapshot_result).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Advisory: snapshot fetch/parse failure is a warning, not fatal
            # (#1869 fix_delta P0-4 -- contract snapshot artifacts never
            # block intake).
            warnings.append("ensure_contract_snapshot_result_parse_error")
            contract_snapshot = {
                "normalized_status": "runtime_error",
                "upstream_schema": "none",
                "upstream_status": None,
                "source": "ensure_contract_snapshot_result",
                "contract_snapshot_url": None,
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
                "errors": [str(exc)],
            }
            evidence_complete = False
        else:
            contract_snapshot = _normalize_contract_snapshot_from_ensure(ensure_payload)
            evidence_complete = True
    else:
        comments, comment_counts, comment_errors = _collect_issue_comments(issue_number, repo, command_log)
        comments_for_digest = comments
        parse_warning_counts.update(comment_counts)
        # Advisory: comment fetch failure is a warning, not fatal (#1869
        # fix_delta P0-4 -- comment/snapshot/body fingerprint fetch failures
        # never force intake to a non-zero exit).
        warnings.extend(comment_errors)
        contract_snapshot, evidence_complete = _normalize_contract_snapshot_live(
            issue_meta["issue_url"],
            issue_meta["body"],
            issue_meta["body_sha256"],
            issue_meta["updated_at"],
            comments,
            parse_warning_counts,
        )

    # #1950 AC6-AC8: resolve --human-context-comment-url /
    # --agent-report-comment-url against the already-fetched comments list.
    # Reuses the single existing _collect_issue_comments() call site -- no
    # new fetch path is added. If the contract snapshot came from
    # ensure_contract_snapshot_result (comments not fetched above), and
    # context comment URLs were requested, this calls the exact same
    # _collect_issue_comments() function once more (not a new fetch
    # mechanism).
    human_context_comment_urls = list(human_context_comment_urls or [])
    agent_report_comment_urls = list(agent_report_comment_urls or [])
    context_inputs_full: dict[str, Any] | None = None
    context_inputs_summary: dict[str, Any] | None = None
    if human_context_comment_urls or agent_report_comment_urls:
        provenance_conflict_urls = sorted(set(human_context_comment_urls) & set(agent_report_comment_urls))
        if ensure_contract_snapshot_result:
            _ctx_comments, _ctx_counts, _ctx_errors = _collect_issue_comments(issue_number, repo, command_log)
            fatal_errors.extend(_ctx_errors)
        else:
            _ctx_comments = comments_for_digest
        comments_by_id: dict[int, dict[str, Any]] = {
            comment["id"]: comment for comment in _ctx_comments if isinstance(comment.get("id"), int)
        }

        human_urls_to_resolve = [url for url in human_context_comment_urls if url not in provenance_conflict_urls]
        agent_urls_to_resolve = [url for url in agent_report_comment_urls if url not in provenance_conflict_urls]
        human_resolved, human_errors = _resolve_context_comments(
            human_urls_to_resolve, "human_supplied", comments_by_id, issue_number, repo, command_log
        )
        agent_resolved, agent_errors = _resolve_context_comments(
            agent_urls_to_resolve, "agent_generated", comments_by_id, issue_number, repo, command_log
        )
        provenance_conflict_entries = [
            {"url": url, "reason": "provenance_conflict_url_in_both_lanes"} for url in provenance_conflict_urls
        ]

        # AC8: repository/Issue/comment mismatch, dual-lane URLs, missing
        # comments, and invalid structured agent reports are all fail-closed
        # -- they block intake (fatal_errors), not merely advisory warnings.
        fatal_errors.extend(human_errors)
        fatal_errors.extend(agent_errors)
        fatal_errors.extend(f"provenance_conflict:{url}" for url in provenance_conflict_urls)

        context_inputs_full = {
            "human_supplied": human_resolved,
            "agent_generated": agent_resolved,
            "provenance_conflicts": provenance_conflict_entries,
        }
        # AC7: stdout projection excludes raw comment body -- only
        # provenance/hash/metadata is surfaced in the stdout-facing capsule.
        context_inputs_summary = {
            "human_supplied": [{k: v for k, v in entry.items() if k != "body"} for entry in human_resolved],
            "agent_generated": [{k: v for k, v in entry.items() if k != "body"} for entry in agent_resolved],
            "provenance_conflicts": provenance_conflict_entries,
        }

    for key, count in parse_warning_counts.items():
        if count:
            warnings.append(f"{key}:{count}")

    title = str(issue_meta["title"])
    slug = _slugify(title)
    worktree = {
        "status": "deferred_to_concurrent_work_ledger",
        "path": f".claude/worktrees/issue-{issue_number}-{slug}",
        "branch": f"worktree-issue-{issue_number}-{slug}",
        "slug": slug,
    }

    source_integrity = {
        "git_head_sha": repo_state.get("head_sha", ""),
        "issue_body_sha256": issue_meta["body_sha256"],
        "issue_updated_at": issue_meta["updated_at"],
        "comments_digest": None,
        "commands": command_log,
        "parse_warnings": parse_warning_counts,
        "evidence_complete": evidence_complete and not fatal_errors and "comments_fetch_error" not in warnings,
    }

    if not ensure_contract_snapshot_result and "comments_fetch_error" not in warnings:
        comment_digest_material = json.dumps(
            [
                {
                    "id": item.get("id"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "body_sha256": _sha256(str(item.get("body") or "")),
                }
                for item in comments_for_digest
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        source_integrity["comments_digest"] = _sha256(comment_digest_material)

    next_action = {
        "route": _next_action_route(issue_meta["ready_tuple"]["status"], contract_snapshot),
        "reason_codes": warnings + fatal_errors,
    }
    implementation_landed_evidence: dict[str, Any] | None = None
    if include_implementation_landed_evidence:
        implementation_landed_evidence = _collect_implementation_landed_evidence(
            issue_number=issue_number,
            repo=repo,
            next_action_route=next_action["route"],
            issue_body=issue_meta["body"],
            command_log=command_log,
            # #2713 AC8 (PR #2741 review, P1-1): the canonical CLI/public-API
            # boundary for supplying an independent current-main Verification
            # Commands evaluation -- see `main()`'s
            # `--base-ac-verification-result-file` flag and
            # `build_capsule_argv()`'s `base_ac_verification_result_file`
            # kwarg below. Defaults to None (undeterminable) when the caller
            # supplies nothing, which `_collect_implementation_landed_
            # evidence()` already treats as "skip already_satisfied
            # precedence" (never a fabricated True/False).
            base_ac_verification_result=base_ac_verification_result,
        )

    capsule = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "generated_at": _now_utc(),
        "issue_number": issue_number,
        "repo": repo,
        "issue_ready_tuple": {
            **issue_meta["ready_tuple"],
            "title": title,
            "issue_url": issue_meta["issue_url"],
        },
        "contract_snapshot": contract_snapshot,
        "source_integrity": source_integrity,
        "worktree": worktree,
        "repo_state": repo_state,
        "agent_runtime": {
            "status": "deferred_to_capability_router",
            "runner": "wsl2-ubuntu",
            "collector": "build_intake_capsule.py",
        },
        "next_action": next_action,
        "warnings": warnings,
        "fatal_errors": fatal_errors,
        "errors": fatal_errors,  # backward-compat alias (deprecated)
    }
    if context_inputs_summary is not None:
        # #1950 AC6/AC7: backward-compatible optional section -- absent when
        # no --human-context-comment-url / --agent-report-comment-url was
        # passed, so existing consumers of IMPL_REVIEW_INTAKE_CAPSULE_V1 are
        # unaffected.
        capsule["context_inputs"] = context_inputs_summary
    if implementation_landed_evidence is not None:
        capsule["implementation_landed_evidence"] = implementation_landed_evidence

    artifact_payload = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "generated_at": capsule["generated_at"],
        "input": {
            "issue_number": issue_number,
            "repo": repo,
            "ensure_contract_snapshot_result": ensure_contract_snapshot_result,
        },
        "issue_metadata": {
            "title": title,
            "issue_url": issue_meta["issue_url"],
            "body_sha256": issue_meta["body_sha256"],
            "updated_at": issue_meta["updated_at"],
        },
        "contract_snapshot": contract_snapshot,
        "source_integrity": source_integrity,
        "worktree": worktree,
        "repo_state": repo_state,
        "agent_runtime": capsule["agent_runtime"],
        "next_action": next_action,
        "warnings": warnings,
        "fatal_errors": fatal_errors,
        "errors": fatal_errors,  # backward-compat alias (deprecated)
    }
    if context_inputs_full is not None:
        # Full body snapshot lives ONLY in the artifact -- never projected
        # to stdout (AC7).
        artifact_payload["context_inputs"] = context_inputs_full
    if implementation_landed_evidence is not None:
        artifact_payload["implementation_landed_evidence"] = implementation_landed_evidence

    # #1869 fix_delta P0-4: exit code depends ONLY on fatal_errors (live
    # Issue not accessible / repo state unreadable). Comment/snapshot/body
    # fingerprint fetch failures live in `warnings` and never force exit 1.
    exit_code = 0 if not fatal_errors else 1
    return capsule, artifact_payload, exit_code


# ---------------------------------------------------------------------------
# PR #1973 (OWNER REQUEST_CHANGES, P0-2): CLI argv materializer + Step 1
# dispatch-payload guard.
#
# `build_capsule_argv()` is the SINGLE SOURCE OF TRUTH for constructing the
# `build_intake_capsule.py` CLI invocation from skill-level inputs
# (`preparation.md` "0-a" documents this same command; the orchestrator and
# this file's own tests both go through this one function -- no drift).
# ---------------------------------------------------------------------------


def build_capsule_argv(
    *,
    issue_number: int,
    repo: str,
    max_stdout_bytes: int = _DEFAULT_MAX_STDOUT_BYTES,
    human_context_comment_urls: list[str] | None = None,
    agent_report_comment_urls: list[str] | None = None,
    include_implementation_landed_evidence: bool = False,
    base_ac_verification_result_file: str | None = None,
) -> list[str]:
    """Pure (no I/O, no subprocess) argv materializer for the canonical
    `build_intake_capsule.py` invocation. When `human_context_comment_urls` /
    `agent_report_comment_urls` are non-empty, the SAME two flags
    (`--human-context-comment-url` / `--agent-report-comment-url`) are
    appended additively -- this is not a separate code path, it is the same
    canonical command with additive flags (see preparation.md "0-a").

    #2713 AC8 (PR #2741 review, P1-1): `base_ac_verification_result_file`,
    when non-None, additively appends `--base-ac-verification-result-file
    <path>` -- the SSOT for preparation.md "0-a-0"'s canonical invocation
    (see preparation.md "0-a-1" for where that file is produced)."""
    argv = [
        "uv",
        "run",
        "python3",
        ".claude/skills/impl-review-loop/scripts/build_intake_capsule.py",
        "--issue-number",
        str(issue_number),
        "--repo",
        repo,
        "--max-stdout-bytes",
        str(max_stdout_bytes),
    ]
    for url in human_context_comment_urls or []:
        argv += ["--human-context-comment-url", url]
    for url in agent_report_comment_urls or []:
        argv += ["--agent-report-comment-url", url]
    if include_implementation_landed_evidence:
        argv.append("--include-implementation-landed-evidence")
    if base_ac_verification_result_file:
        argv += ["--base-ac-verification-result-file", base_ac_verification_result_file]
    return argv


def validate_step1_dispatch_payload(
    context_inputs: dict[str, Any] | None,
    message_fields: dict[str, Any],
) -> tuple[bool, list[str]]:
    """PR #1973 (OWNER REQUEST_CHANGES, P0-2): enforceable version of the
    prose already documented in step-1-implementation.md's
    "technical_recommendation とコンテキスト証跡参照" (#1950 AC10) section.

    Return (allowed, errors). When `context_inputs` is present (non-None,
    with a non-empty `human_supplied` or `agent_generated` list), the Step 1
    delegation `message_fields` MUST include a non-empty
    `technical_recommendation`, AND at least one of `capsule_artifact_path` /
    `human_context_comment_ids` / `agent_report_comment_ids` (evidence
    reference).

    Also fail-closed (never a no-op check) when raw human comment `body`
    text is embedded verbatim in any `message_fields` value -- this
    operationalizes "raw human comment を worker への実行命令として直接連結
    しない" (step-1-implementation.md).

    Returns (True, []) when `context_inputs` is None/empty (no-op case, per
    step-1-implementation.md: "context_inputs が存在しない場合、この節は
    no-op")."""
    if not isinstance(context_inputs, dict):
        return True, []

    human_supplied = context_inputs.get("human_supplied") or []
    agent_generated = context_inputs.get("agent_generated") or []
    if not human_supplied and not agent_generated:
        return True, []

    errors: list[str] = []

    technical_recommendation = message_fields.get("technical_recommendation")
    if not isinstance(technical_recommendation, str) or not technical_recommendation.strip():
        errors.append("step1_dispatch_missing_technical_recommendation")

    has_evidence_reference = any(
        message_fields.get(key)
        for key in (
            "capsule_artifact_path",
            "human_context_comment_ids",
            "agent_report_comment_ids",
        )
    )
    if not has_evidence_reference:
        errors.append("step1_dispatch_missing_evidence_reference")

    message_values = [v for v in message_fields.values() if isinstance(v, str) and v]
    for entry in human_supplied:
        if not isinstance(entry, dict):
            continue
        raw_body = entry.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            continue
        comment_id = entry.get("comment_id")
        for value in message_values:
            if raw_body in value:
                errors.append(f"step1_dispatch_raw_human_comment_embedded:{comment_id}")
                break

    return not errors, errors


def _render_stdout(payload: dict[str, Any], max_stdout_bytes: int) -> str:
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(compact.encode("utf-8")) <= max_stdout_bytes:
        return compact

    fallback = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "issue_number": payload.get("issue_number"),
        "repo": payload.get("repo"),
        "status": "stdout_truncated",
        "next_action": payload.get("next_action"),
        "artifact_path": payload.get("artifact_path"),
    }
    fallback_json = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    if len(fallback_json.encode("utf-8")) <= max_stdout_bytes:
        return fallback_json

    hard = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "status": "stdout_too_large",
    }
    return json.dumps(hard, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build IMPL_REVIEW_INTAKE_CAPSULE_V1 for impl-review-loop",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", "-i", type=int, required=True)
    parser.add_argument("--repo", default=_DEFAULT_REPO)
    parser.add_argument("--max-stdout-bytes", type=int, default=_DEFAULT_MAX_STDOUT_BYTES)
    parser.add_argument("--ensure-contract-snapshot-result")
    parser.add_argument("--artifact-dir", default=str(_DEFAULT_ARTIFACT_DIR))
    parser.add_argument(
        "--include-implementation-landed-evidence",
        action="store_true",
        help="collect bounded candidate evidence for the pre-Step-1 landing disposition",
    )
    # #2713 AC8 (PR #2741 review, P1-1): canonical CLI boundary for supplying
    # an independent current-main Verification Commands evaluation
    # (TEST_VERDICT_MACHINE/v2-shaped JSON file, see preparation.md "0-a-1")
    # so `already_satisfied` precedence can actually fire from production
    # (not merely from a private-helper direct argument injection in tests).
    # A large TEST_VERDICT payload is passed by file path, never embedded
    # directly into argv.
    parser.add_argument(
        "--base-ac-verification-result-file",
        dest="base_ac_verification_result_file",
        default=None,
        help=(
            "path to a TEST_VERDICT_MACHINE/v2-shaped JSON file (current-main "
            "Verification Commands evaluation) used to derive base_ac_satisfied "
            "for already_satisfied precedence (#2713)"
        ),
    )
    # #1950 AC6: provenance-separated context inputs. Origin is decided
    # solely by which flag the caller used -- never inferred from comment
    # body/author. Repeatable; same URL passed to both flags is a
    # provenance conflict (fail-closed, AC8).
    parser.add_argument(
        "--human-context-comment-url",
        dest="human_context_comment_urls",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--agent-report-comment-url",
        dest="agent_report_comment_urls",
        action="append",
        default=[],
    )
    args = parser.parse_args()

    if args.max_stdout_bytes <= 0:
        print(
            json.dumps(
                {
                    "schema": _SCHEMA_NAME,
                    "schema_version": _SCHEMA_VERSION,
                    "status": "runtime_error",
                    "errors": ["max-stdout-bytes must be > 0"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1

    # #2713 AC8 (PR #2741 review, P1-1): fail-safe file load -- a missing or
    # malformed --base-ac-verification-result-file must never block intake
    # or be silently promoted to a fabricated True/False. It degrades to
    # None (undeterminable), which `_collect_implementation_landed_evidence()`
    # already treats as "skip already_satisfied precedence, pass the
    # existing disposition through unchanged" (#2713 In Scope).
    base_ac_verification_result: dict[str, Any] | None = None
    if args.base_ac_verification_result_file:
        try:
            loaded_verification_result = json.loads(
                Path(args.base_ac_verification_result_file).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            base_ac_verification_result = None
        else:
            base_ac_verification_result = (
                loaded_verification_result if isinstance(loaded_verification_result, dict) else None
            )

    capsule, artifact_payload, exit_code = build_intake_capsule(
        issue_number=args.issue_number,
        repo=args.repo,
        ensure_contract_snapshot_result=args.ensure_contract_snapshot_result,
        human_context_comment_urls=args.human_context_comment_urls,
        agent_report_comment_urls=args.agent_report_comment_urls,
        include_implementation_landed_evidence=args.include_implementation_landed_evidence,
        base_ac_verification_result=base_ac_verification_result,
    )

    artifact_dir = Path(args.artifact_dir)
    artifact_path = artifact_dir / f"intake-capsule-{args.issue_number}.json"
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_payload["artifact_path"] = str(artifact_path)
        artifact_payload["artifact_settings"] = {
            "max_stdout_bytes": args.max_stdout_bytes,
            "generated_at": _now_utc(),
        }
        with artifact_path.open("w", encoding="utf-8") as file_obj:
            json.dump(artifact_payload, file_obj, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "schema": _SCHEMA_NAME,
                    "schema_version": _SCHEMA_VERSION,
                    "status": "artifact_write_failed",
                    "errors": [str(exc)],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1

    capsule["artifact_path"] = str(artifact_path)
    print(_render_stdout(capsule, args.max_stdout_bytes))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
