#!/usr/bin/env python3
"""
publish_termination_report.py

Thin publisher that posts a plain-markdown termination summary body to a
GitHub issue as a comment, via the issue_comment.publish controlled
mutation lane.

#1873 (bounded review loops): the previous version of this module invoked
render_termination_report.py (a TERMINATION_REPORT_INPUT_V1 -> rendered-body
pipeline layered on top of PARENT_REPLAY_* / ISSUE_EXECUTION_DECISION_V1
routing state) as a subprocess, then posted its output. That renderer and
the routing state it depended on have been removed. The orchestrator
(`plan_refinement_loop.py` / SKILL.md Step 5) now assembles a short plain
markdown summary directly and passes it to this module as-is -- there is no
intermediate render/validate step here.

Usage:
    python3 publish_termination_report.py \
        --issue-number <int> \
        --repo <owner/repo> \
        [--body-file <path>]

Input:
    The comment body (plain markdown text), from stdin or --body-file.

Output:
    Artifact logged to stderr / local artifact file on failure.
    On normal success: posts GitHub comment via the issue_comment.publish
    controlled mutation lane (never a raw `gh issue comment` call).
    With --dry-run: validates without posting or emitting a Task Context signal.

Exit codes:
    0 - comment posted successfully, or dry-run validation succeeded
    1 - failure (fail-closed, gh not called, or gh call failed)
    2 - usage error / missing required arguments / empty body
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Issue #1633 / #1639 / #1873 Tranche 3: the isolation worktree agent's
# bounded Issue comment request producer/consumer (build/materialize) and
# the controlled-executor invocation helper now live in
# isolation_issue_comment_bridge.py, a standalone module shared by any
# production caller of the issue_comment.publish lane -- not duplicated
# here. This module imports them rather than redefining them.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR_FOR_BRIDGE = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR_FOR_BRIDGE) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR_FOR_BRIDGE))

from isolation_issue_comment_bridge import (  # noqa: E402
    build_isolation_issue_comment_request,
    materialize_isolation_issue_comment_request,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_AGENT_GUARDS_DIR = _PROJECT_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from controlled_skill_mutation_policy import (  # noqa: E402
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
)

CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT = _AGENT_GUARDS_DIR / "controlled_skill_mutation_exec.py"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Artifact directory relative to cwd (or absolute via env var)
ARTIFACT_DIR = Path(os.environ.get("PUBLISH_ARTIFACT_DIR", "artifacts"))

# Issue #1908: this is a mode of the existing controlled publisher, not a new
# GitHub transport.  The identity and marker are deliberately self-contained
# in the new human-history comment so legacy machine-readable comments retain
# their target, marker, payload, and consumers unchanged.
_HUMAN_HISTORY_FIELDS = (
    "loop_kind",
    "phase",
    "source_issue_number",
    "target_kind",
    "target_number",
    "route_or_termination_reason",
    "reviewed_ref",
)
_HUMAN_HISTORY_REASONS = frozenset(
    {
        "completed",
        "needs_fix",
        "human_judgment",
        "binding_missing",
        "binding_ambiguous",
        "binding_wrong_repo",
        "binding_gone",
        "head_drift",
        "human_escalation",
    }
)
_HUMAN_HISTORY_MARKER_PREFIX = "<!-- loop-protocol/human-history:v1:sha256:"
_HUMAN_HISTORY_MARKER_RE = __import__("re").compile(r"^<!-- loop-protocol/human-history:v1:sha256:[0-9a-f]{64} -->$")
_HUMAN_HISTORY_ISSUE_REF_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
_HUMAN_HISTORY_PR_REF_RE = __import__("re").compile(r"^refs/pull/([1-9][0-9]*)/head@([0-9a-f]{40})$")
_HUMAN_HISTORY_HEAD_SHA_RE = __import__("re").compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_SECRET_OR_UNSAFE_RE = __import__("re").compile(
    r"(?:"
    r"gh[porsu]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|authorization:\s*bearer"
    r"|(?:^|\s)/[^\s]+"
    # Windows drive-absolute path (e.g. C:\Users\...).
    r"|[A-Za-z]:\\\S+"
    # UNC path (e.g. \\server\share\...).
    r"|\\\\\S+\\\S+"
    # Explicit credential assignment (password=, api_key:, token=, ...).
    r"|(?:password|passwd|pwd|api[_-]?key|secret|access[_-]?key|token)\s*[:=]\s*\S+"
    r")",
    __import__("re").I,
)


def _human_history_jcs(identity: dict) -> bytes:
    """The identity value domain is ASCII-only, so sorted compact JSON is the
    RFC 8785 JCS representation for this fixed object shape."""
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_human_history_identity(identity: object) -> tuple[dict | None, str]:
    if not isinstance(identity, dict) or set(identity) != set(_HUMAN_HISTORY_FIELDS):
        return None, "human_history_identity_key_set_invalid"
    normalized = {name: identity[name] for name in _HUMAN_HISTORY_FIELDS}
    for field in ("loop_kind", "phase", "target_kind", "route_or_termination_reason", "reviewed_ref"):
        if not isinstance(normalized[field], str):
            return None, f"human_history_identity_type_invalid:{field}"
    for field in ("source_issue_number", "target_number"):
        if type(normalized[field]) is not int or normalized[field] <= 0:
            return None, f"human_history_identity_type_invalid:{field}"
    loop = normalized["loop_kind"]
    phase = normalized["phase"]
    target_kind = normalized["target_kind"]
    reason = normalized["route_or_termination_reason"]
    if loop not in {"issue-refinement-loop", "impl-review-loop"} or reason not in _HUMAN_HISTORY_REASONS:
        return None, "human_history_identity_literal_invalid"
    valid = {
        ("issue-refinement-loop", "review-complete", "issue"): {"completed", "needs_fix", "human_judgment"},
        ("impl-review-loop", "pre-PR-binding", "issue"): {"completed", "needs_fix", "human_judgment"},
        ("impl-review-loop", "binding-validation", "issue"): {
            "binding_missing",
            "binding_ambiguous",
            "binding_wrong_repo",
            "binding_gone",
        },
        ("impl-review-loop", "post-PR-binding", "pull_request"): {"completed", "needs_fix", "human_judgment"},
        ("impl-review-loop", "post-PR-head-drift", "pull_request"): {"head_drift"},
        ("impl-review-loop", "conflict-resolution", "issue"): {"human_escalation"},
        ("impl-review-loop", "conflict-resolution", "pull_request"): {"human_escalation"},
    }
    if reason not in valid.get((loop, phase, target_kind), set()):
        return None, "human_history_identity_matrix_combination_invalid"
    if target_kind == "issue":
        if normalized["target_number"] != normalized["source_issue_number"]:
            return None, "human_history_issue_target_number_mismatch"
        if not _HUMAN_HISTORY_ISSUE_REF_RE.fullmatch(normalized["reviewed_ref"]):
            return None, "human_history_issue_reviewed_ref_invalid"
    else:
        reference = _HUMAN_HISTORY_PR_REF_RE.fullmatch(normalized["reviewed_ref"])
        if reference is None or int(reference.group(1)) != normalized["target_number"]:
            return None, "human_history_pr_reviewed_ref_invalid"
    return normalized, ""


def _validate_public_safe_human_text(value: object, field: str) -> tuple[str | None, str]:
    if not isinstance(value, str) or not value.strip() or "```" in value or _SECRET_OR_UNSAFE_RE.search(value):
        return None, f"human_history_public_safe_text_invalid:{field}"
    return value.strip(), ""


def render_human_history_comment(
    *,
    identity: object,
    result: object,
    evidence_refs: object,
    recommended_action: object,
    recommended_reason: object,
    impact_if_unaddressed: object,
    stale_evidence: object = None,
) -> tuple[dict | None, str]:
    """Build a public-safe Japanese human-history body and stable marker."""
    value, error = _validate_human_history_identity(identity)
    if error:
        return None, error
    assert value is not None
    text_values: dict[str, str] = {}
    for name, raw in {
        "result": result,
        "recommended_action": recommended_action,
        "recommended_reason": recommended_reason,
        "impact_if_unaddressed": impact_if_unaddressed,
    }.items():
        clean, text_error = _validate_public_safe_human_text(raw, name)
        if text_error:
            return None, text_error
        assert clean is not None
        text_values[name] = clean
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return None, "human_history_evidence_refs_invalid"
    evidence: list[str] = []
    for item in evidence_refs:
        clean, text_error = _validate_public_safe_human_text(item, "evidence_ref")
        if text_error:
            return None, text_error
        assert clean is not None
        evidence.append(clean)
    stale_line = ""
    is_head_drift = value["phase"] == "post-PR-head-drift"
    if is_head_drift and stale_evidence is None:
        return None, "human_history_head_drift_stale_evidence_missing"
    if stale_evidence is not None:
        clean, text_error = _validate_public_safe_human_text(stale_evidence, "stale_evidence")
        if text_error:
            return None, text_error
        # The controlled executor makes the authoritative direct PR-head read
        # immediately before its create/PATCH/noop decision.  Require exactly
        # one public-safe latest-head value here so a diagnostic can be
        # reconciled under its existing stable identity rather than using an
        # unbound prose-only stale transition.
        if is_head_drift and len(_HUMAN_HISTORY_HEAD_SHA_RE.findall(clean)) != 1:
            return None, "human_history_head_drift_stale_evidence_head_invalid"
        stale_line = f"\n- stale evidence: {clean}"
    identity_sha256 = hashlib.sha256(_human_history_jcs(value)).hexdigest()
    marker = f"{_HUMAN_HISTORY_MARKER_PREFIX}{identity_sha256} -->"
    body = (
        "## review loop の作業履歴\n\n"
        f"- 実施内容: {text_values['result']}\n"
        f"- phase: {value['phase']}\n"
        f"- reviewed_ref: {value['reviewed_ref']}\n"
        f"- 推奨アクション: {text_values['recommended_action']}\n"
        f"- 推奨する理由: {text_values['recommended_reason']}\n"
        f"- 対応しない場合の影響: {text_values['impact_if_unaddressed']}\n"
        f"- evidence refs: {'; '.join(evidence)}{stale_line}\n\n{marker}\n"
    )
    return {
        "body": body,
        "marker": marker,
        "identity": value,
        "identity_sha256": identity_sha256,
    }, ""


def publish_human_history(
    *,
    target_number: int,
    repo: str,
    identity: object,
    result: object,
    evidence_refs: object,
    recommended_action: object,
    recommended_reason: object,
    impact_if_unaddressed: object,
    stale_evidence: object = None,
    dry_run: bool = False,
    receipt: dict | None = None,
) -> int:
    """Publish one human-history event through existing issue_comment.publish.

    `receipt`, when passed a mutable dict, is populated with the controlled
    executor's structured CONTROLLED_SKILL_MUTATION_RESULT_V1 fields
    (including the raw `status_detail`, e.g. `created` / `updated` /
    `already_published` / `dry_run_ok`) so a caller can assert on the actual
    remote operation semantics rather than only the int exit code (Issue
    #1908 fix_delta HIGH-2). The exit code remains the success/failure
    transport contract; it is unchanged for callers that don't pass
    `receipt`.
    """
    rendered, error = render_human_history_comment(
        identity=identity,
        result=result,
        evidence_refs=evidence_refs,
        recommended_action=recommended_action,
        recommended_reason=recommended_reason,
        impact_if_unaddressed=impact_if_unaddressed,
        stale_evidence=stale_evidence,
    )
    if error or rendered is None:
        _record_artifact(issue_number=target_number, reason_code=error or "human_history_render_failed")
        return 1
    if type(target_number) is not int or target_number <= 0 or rendered["identity"]["target_number"] != target_number:
        _record_artifact(issue_number=target_number, reason_code="human_history_target_binding_invalid")
        return 1
    return _post_github_comment(
        issue_number=target_number,
        body=rendered["body"],
        repo=repo,
        marker=rendered["marker"],
        dry_run=dry_run,
        receipt=receipt,
    )


# ---------------------------------------------------------------------------
# Artifact logging (fail-closed: logs to local file, never leaks body to stderr)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_artifact(
    *,
    issue_number: int | None,
    reason_code: str | None,
    extra: dict | None = None,
) -> None:
    """
    Record failure artifact to local file.

    IMPORTANT: Does NOT write the comment body to stderr or any log.
    Only reason_code and issue_number (and non-body extras) are recorded.
    """
    timestamp = _now_iso()
    artifact = {
        "timestamp": timestamp,
        "issue_number": issue_number,
        "reason_code": reason_code,
    }
    if extra:
        artifact.update(extra)

    print(
        f"[publish_termination_report] reason_code={reason_code!r}",
        file=sys.stderr,
    )

    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        artifact_path = ARTIFACT_DIR / f"termination_report_publish_{timestamp.replace(':', '-')}.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
        print(f"[publish_termination_report] artifact written: {artifact_path}", file=sys.stderr)
    except Exception as exc:
        print(f"[publish_termination_report] failed to write artifact: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# GitHub comment posting (fail-closed)
# ---------------------------------------------------------------------------


def _post_github_comment(
    *,
    issue_number: int,
    body: str,
    repo: str,
    marker: str | None = None,
    dry_run: bool = False,
    receipt: dict | None = None,
) -> int:
    """
    Post body as a GitHub issue comment via the issue_comment.publish
    controlled mutation lane (Issue #1633).

    Builds a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request (via
    build_isolation_issue_comment_request(), Issue #1639 fix_delta P1-1)
    from body (embedding CONTROLLED_EXEC_MARKER from env, or a deterministic
    marker derived from repo + issue_number + body when unset -- Issue #1639
    fix_delta P1-2 -- as the request's marker field), materializes it via
    materialize_isolation_issue_comment_request(), and launches
    controlled_skill_mutation_exec.py --command-id
    issue_comment.publish with the exact argv it accepts (Issue #1166
    AC4/AC17 shared authority -- raw `gh issue comment` is never called
    directly from this module).
    Enforces a 30-second timeout; on timeout fails closed.

    `dry_run` forwards --dry-run to the controlled executor (validate/render
    only, no remote mutation). `receipt`, when a mutable dict is passed, is
    populated with the executor's parsed CONTROLLED_SKILL_MUTATION_RESULT_V1
    JSON (requested via --json) so a caller can read the raw `status_detail`
    (Issue #1908 fix_delta HIGH-2) instead of only the exit code.

    Returns the executor's exit code (0 on success, -1 on timeout, or the
    executor's nonzero exit on failure).
    """
    if marker is None:
        exec_marker = os.environ.get("CONTROLLED_EXEC_MARKER", "")
        if exec_marker:
            marker = f"<!-- CONTROLLED_EXEC_MARKER:{exec_marker} -->"
        else:
            # Issue #1639 fix_delta P1-2: the fallback marker must not collide
            # across different repos/issues that happen to share identical body.
            fallback_seed = f"{repo}\x00{issue_number}\x00{body}".encode("utf-8")
            content_hash = hashlib.sha256(fallback_seed).hexdigest()[:32]
            marker = f"<!-- CONTROLLED_EXEC_MARKER:{content_hash} -->"
        comment_body = body + f"\n{marker}"
    else:
        # #1908 passes its stable identity marker intact to the existing bridge;
        # adding a legacy marker would alter the new comment's digest semantics.
        if marker not in body:
            return 1
        comment_body = body

    request = build_isolation_issue_comment_request(
        issue_number=issue_number,
        repo=repo,
        comment_body=comment_body,
        marker=marker,
    )
    materialized_rel_path, materialize_err = materialize_isolation_issue_comment_request(
        request=request,
        expected_issue_number=issue_number,
        expected_repo=repo,
        project_root=_PROJECT_ROOT,
    )
    if materialize_err:
        print(
            f"[publish_termination_report] materialize_isolation_issue_comment_request failed: {materialize_err}",
            file=sys.stderr,
        )
        return 1

    cmd = [
        sys.executable,
        str(CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT),
        "--command-id",
        COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        "--issue-number",
        str(issue_number),
        "--input-file",
        materialized_rel_path,
        "--repo",
        repo,
        "--json",
    ]
    if dry_run:
        cmd.append("--dry-run")
    env = os.environ.copy()
    # The controlled lane binds its CLI number to the *comment target*.  A PR
    # history event legitimately carries a distinct source Issue identity, so
    # an inherited source-issue session binding must not mis-bind this target.
    if marker.startswith(_HUMAN_HISTORY_MARKER_PREFIX):
        env.pop("LOOP_ISSUE_NUMBER", None)
    env["GH_PROMPT_DISABLED"] = "1"
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(
            "[publish_termination_report] controlled_skill_mutation_exec issue_comment.publish "
            "timed out (30s) — fail-closed",
            file=sys.stderr,
        )
        return -1

    if receipt is not None:
        try:
            parsed_receipt = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            parsed_receipt = None
        if isinstance(parsed_receipt, dict):
            receipt.update(parsed_receipt)

    if proc.returncode != 0:
        print(
            f"[publish_termination_report] controlled_skill_mutation_exec "
            f"issue_comment.publish failed (exit {proc.returncode}): {proc.stderr[:200]}",
            file=sys.stderr,
        )
    return proc.returncode


# ---------------------------------------------------------------------------
# Main publish flow
# ---------------------------------------------------------------------------


def _emit_refinement_approved_signal(
    *, repo: str, issue_number: int, approved_body_sha256: str | None
) -> tuple[str, str]:
    """Post-publish best-effort adapter; it cannot undo a published handoff.

    Returns ``(disposition, reason_code)`` the same way
    ``open_pr.emit_implementation_pr_observed()`` already does. The
    canonical approved-handoff publish above has already succeeded and
    stays independent of this outcome -- this return value only lets the
    caller surface a silent Task Context sync failure (e.g. ``deferred`` /
    ``unbound`` from a stale origin session) instead of discarding it, so it
    no longer only shows up later as a confusing downstream
    ``activity_missing``. Never fail-closed, never rolled back.
    """
    origin = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not origin:
        return "deferred", "unbound"
    if not isinstance(approved_body_sha256, str) or __import__("re").fullmatch(
        r"[0-9a-f]{64}", approved_body_sha256
    ) is None:
        return "rejected_evidence", "INVALID_APPROVED_BODY_SHA256"
    payload = {
        "signal_kind": "refinement_approved",
        "source": "issue-refinement-loop",
        "source_schema_version": "v1",
        "evidence": {"repo": repo.lower(), "issue_number": issue_number, "approved_body_sha256": approved_body_sha256},
    }
    ctl = _PROJECT_ROOT / "scripts" / "task-context" / "task_contextctl.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(ctl), "signal", "apply"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=10,
        )
        data = json.loads(proc.stdout.splitlines()[-1]) if proc.stdout.splitlines() else {}
        result = data.get("data", {}) if isinstance(data, dict) else {}
        return str(result.get("disposition", "deferred")), str(result.get("reason_code", "ADAPTER_UNAVAILABLE"))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, IndexError):
        return "deferred", "ADAPTER_UNAVAILABLE"


def publish(
    *,
    issue_number: int,
    body: str,
    repo: str,
    termination_reason: str | None = None,
    approved_body_sha256: str | None = None,
    dry_run: bool = False,
) -> int:
    """Publish a comment, or validate it without posting when dry_run is set."""
    if not isinstance(body, str) or not body.strip():
        _record_artifact(issue_number=issue_number, reason_code="empty_body")
        return 1

    gh_exit = _post_github_comment(issue_number=issue_number, body=body, repo=repo, dry_run=dry_run)
    if gh_exit != 0:
        reason = "gh_comment_timeout" if gh_exit == -1 else "gh_comment_failed"
        _record_artifact(
            issue_number=issue_number,
            reason_code=reason,
            extra={"gh_exit_code": gh_exit},
        )
        return 1

    if dry_run:
        print(
            f"[publish_termination_report] dry-run validation-only for issue #{issue_number}; no GitHub mutation",
            file=sys.stderr,
        )
        return 0

    if termination_reason == "approved":
        signal_disposition, signal_reason = _emit_refinement_approved_signal(
            repo=repo, issue_number=issue_number, approved_body_sha256=approved_body_sha256
        )
        print(
            f"[publish_termination_report] task_context refinement_approved signal "
            f"disposition={signal_disposition!r} reason_code={signal_reason!r}",
            file=sys.stderr,
        )
        # The canonical approved-handoff publish above already succeeded and
        # stays independent of this outcome (never fail-closed, never
        # rolled back). Anything other than a clean applied/duplicate
        # outcome is recorded to the existing artifact log so a stale-origin
        # sync failure is diagnosable here instead of only surfacing later
        # as a confusing downstream `activity_missing`.
        if signal_disposition not in {"applied", "duplicate_noop"}:
            _record_artifact(
                issue_number=issue_number,
                reason_code="task_context_signal_not_applied",
                extra={"signal_disposition": signal_disposition, "signal_reason_code": signal_reason},
            )
    print(
        f"[publish_termination_report] comment posted for issue #{issue_number}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Human-history production entrypoint (Issue #1908 fix_delta BLOCKER)
#
# This is the exact, single production call site that turns a structured
# HUMAN_HISTORY_PUBLISH_REQUEST_V1 request into publish_human_history() ->
# render_human_history_comment() -> the existing issue_comment.publish
# controlled lane. It is not a second publisher, event_ledger, run_id_store,
# or hook -- it dispatches to the same publish_human_history() any other
# caller uses. It never replaces or bypasses the legacy plain
# --issue-number/--repo/--body-file mode below.
# ---------------------------------------------------------------------------

_HUMAN_HISTORY_REQUEST_SCHEMA = "HUMAN_HISTORY_PUBLISH_REQUEST_V1"
_HUMAN_HISTORY_REQUEST_REQUIRED_KEYS = frozenset(
    {
        "identity",
        "result",
        "evidence_refs",
        "recommended_action",
        "recommended_reason",
        "impact_if_unaddressed",
    }
)
_HUMAN_HISTORY_REQUEST_OPTIONAL_KEYS = frozenset({"stale_evidence"})


def _run_human_history_request_cli(*, request_path: str, repo: str, dry_run: bool) -> int:
    """CLI-facing production entrypoint for a HUMAN_HISTORY_PUBLISH_REQUEST_V1
    JSON file. Reads/validates the structured request, then calls
    publish_human_history() exactly as any other production caller would.

    Prints a single-line JSON receipt (status_detail / exit_code only --
    never the rendered body) to stdout so a subprocess caller (including a
    test harness) can assert on the actual controlled-lane outcome.

    Exit codes:
        0 - publish_human_history() succeeded (including dry-run validation)
        1 - publish_human_history() failed (fail-closed)
        2 - usage error: request file missing/unreadable/malformed
    """
    try:
        raw = Path(request_path).read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[publish_termination_report] failed to read human-history request file: {exc}", file=sys.stderr)
        return 2
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[publish_termination_report] human-history request file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(request, dict):
        print("[publish_termination_report] human-history request must be a JSON object", file=sys.stderr)
        return 2

    keys = set(request)
    missing = _HUMAN_HISTORY_REQUEST_REQUIRED_KEYS - keys
    unknown = keys - _HUMAN_HISTORY_REQUEST_REQUIRED_KEYS - _HUMAN_HISTORY_REQUEST_OPTIONAL_KEYS
    if missing or unknown:
        print(
            f"[publish_termination_report] human-history request key set invalid "
            f"(missing={sorted(missing)}, unknown={sorted(unknown)})",
            file=sys.stderr,
        )
        return 2

    identity = request["identity"]
    if not isinstance(identity, dict):
        print("[publish_termination_report] human-history request identity must be an object", file=sys.stderr)
        return 2
    target_number = identity.get("target_number")
    if type(target_number) is not int or target_number <= 0:
        print("[publish_termination_report] human-history request identity.target_number is invalid", file=sys.stderr)
        return 2

    receipt: dict = {}
    exit_code = publish_human_history(
        target_number=target_number,
        repo=repo,
        identity=identity,
        result=request["result"],
        evidence_refs=request["evidence_refs"],
        recommended_action=request["recommended_action"],
        recommended_reason=request["recommended_reason"],
        impact_if_unaddressed=request["impact_if_unaddressed"],
        stale_evidence=request.get("stale_evidence"),
        dry_run=dry_run,
        receipt=receipt,
    )
    status_detail = receipt.get("status_detail")
    print(
        f"[publish_termination_report] human-history publish exit={exit_code} status_detail={status_detail!r}",
        file=sys.stderr,
    )
    # routing-relevant fields only -- never the rendered comment body.
    print(json.dumps({"status_detail": status_detail, "exit_code": exit_code}, ensure_ascii=False))
    return 0 if exit_code == 0 else 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a plain-markdown termination summary to a GitHub issue comment."
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="GitHub issue number to comment on (legacy plain-body mode)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="GitHub repository slug (owner/repo) for canonical repo binding",
    )
    parser.add_argument(
        "--body-file",
        type=str,
        default=None,
        help="Path to a plain markdown body file (default: stdin; legacy plain-body mode)",
    )
    parser.add_argument(
        "--termination-reason",
        choices=("approved", "needs_fix", "human_judgment"),
        default=None,
        help="Canonical termination reason; only approved emits refinement_approved after publish.",
    )
    parser.add_argument(
        "--approved-body-sha256",
        default=None,
        help="SHA-256 of the persisted approved Issue body (required to emit approved signal).",
    )
    parser.add_argument(
        "--human-history-request-file",
        type=str,
        default=None,
        help=(
            "Path to a HUMAN_HISTORY_PUBLISH_REQUEST_V1 JSON file (Issue #1908 "
            "production entrypoint). Mutually exclusive with legacy plain-body mode."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without posting or signaling (forwarded to the controlled executor).",
    )
    args = parser.parse_args()

    if args.human_history_request_file:
        return _run_human_history_request_cli(
            request_path=args.human_history_request_file,
            repo=args.repo,
            dry_run=args.dry_run,
        )

    if args.issue_number is None:
        print(
            "[publish_termination_report] --issue-number is required for legacy plain-body mode "
            "(or pass --human-history-request-file)",
            file=sys.stderr,
        )
        return 2

    # Read input
    if args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[publish_termination_report] failed to read body file: {exc}", file=sys.stderr)
            return 2
    else:
        body = sys.stdin.read()

    if not body.strip():
        print("[publish_termination_report] body is empty", file=sys.stderr)
        return 2

    return publish(
        issue_number=args.issue_number,
        body=body,
        repo=args.repo,
        termination_reason=args.termination_reason,
        approved_body_sha256=args.approved_body_sha256,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
