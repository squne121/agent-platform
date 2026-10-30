#!/usr/bin/env python3
"""dependency_materializer.py — Issue #2435.

Single common dependency-materialization choke point for issue-refinement-loop.

## Background

Issue #2424's refinement completed with three confirmed hard predecessors
recorded only as Issue-body prose (a human-readable mirror/fallback). The
GitHub native predecessor relationship (SSOT per `docs/dev/github-ops.md`)
was never materialized, so a later live readback found it empty while the
body still claimed the predecessors were tracked. This module exists so
that no production lane (trusted human context, trusted anchor, controlled
reframe, or the #2406 confirmed-hard-predecessor route) can report a hard
dependency as "recorded" without an accompanying, independently-verified
native materialization.

## Scope (Issue #2435)

This module is a producer + materializer, not a new mutation surface:

- The *producer* functions project an already-confirmed
  ``ISSUE_EXECUTION_DECISION_V1`` semantic decision (``relations[]`` with
  ``relation_type == "depends_on"`` and ``execution.predecessors[]``) into a
  desired native-predecessor set for a single target Issue. They never
  invent a new dependency-classification rule; they only read a decision
  that some other trusted lane already confirmed.
- The *materializer* functions turn a desired set (plus an explicit,
  caller-declared stale-predecessor removal list) into the existing
  ``.claude/skills/edit-issue/scripts/edit_issue_txn.py`` /
  ``issue_relationship.update`` controlled-executor request. No GraphQL/REST
  call to GitHub's dependency endpoints is made directly by this module;
  every remote read or write is delegated to ``edit_issue_txn.py`` (AC10).

Removal is **never** derived from "live native state minus desired state"
(a full-set-replace computation would silently drop an unrelated,
pre-existing native predecessor nobody in this pipeline ever confirmed --
see ``derive_stale_predecessors`` and the AC7(b) regression fixture). It is
always the explicit set-difference between two *confirmed decision*
snapshots (previous vs current), or an explicit caller-declared list.

## Failure classification (AC8)

``classify_materialization_failure`` distinguishes:

- ``native-capability-unavailable``: the ``gh`` binary itself is missing.
- ``auth-or-environment-failure``: ``gh`` is present but auth/network is
  unreachable (routine/retryable environment failure -- never converted
  into an unnecessary human approval, but also never reported as success).
- ``controlled-executor-failure``: the ``issue_relationship.update``
  controlled executor itself failed (transport, receipt loss, etc.).
- ``readback-mismatch``: a live readback did not match the expected
  postcondition (drift before or after mutation).
- ``semantic-human-judgment-required``: a graph-invariant violation or a
  forwarded ``human_judgment`` readiness status -- a genuine judgment call,
  not a retryable environment failure.

## Reuse by other lanes (AC9)

Every public function here is a plain, side-effect-free (or clearly-scoped
I/O) callable with an explicit, injectable collaborator boundary
(``capability_preflight`` / ``fetch_live_predecessors`` /
``invoke_edit_issue_txn``). The #2406 confirmed-hard-predecessor route (or
any future lane) can import and call ``materialize_dependencies`` directly
without needing its own copy of the delta/postcondition logic, and without
colliding with this module's own callers.

## Readiness consumption, not re-computation (P1, OWNER review on PR #2447)

``materialize_dependencies`` never invokes ``contract_readiness_check.py``
itself. It only forwards whatever ``readiness_result`` the caller already
computed earlier in the same refinement cycle (e.g. the existing Step 4
pre-edit static readiness check, or the Final Gate's confirmed
``CONTRACT_REVIEW_RESULT_V1.status == "go"``) to
``edit_issue_txn.py``'s existing ``readiness_forwarding_payload`` contract.
This module previously ran its own ``contract_readiness_check.py``
subprocess and mapped that subprocess's own non-JSON/crash output to
``status: input_or_runtime_error``, which ``edit_issue_txn.py`` then folds
into an overall ``status: human_judgment`` -- turning a routine, retryable
failure inside *this module's own* readiness re-check into an unnecessary
human-judgment escalation. Removing the internal re-check removes that
failure mode entirely: a caller-supplied ``readiness_result`` whose own
``status`` is genuinely ``human_judgment`` / ``input_or_runtime_error`` (a
real signal from the workflow's own readiness pipeline, not a crash inside
this module) is still correctly classified as
``semantic-human-judgment-required`` via
``_FAILURE_CLASS_BY_ERROR_CODE["readiness_forwarding_requires_human_judgment"]``.

## Step 5 termination gate (P0, OWNER review on PR #2447)

``evaluate_termination_dependency_gate`` is the single production choke
point the ``issue-refinement-loop`` Step 5 termination path calls before
posting an ``approved`` / ``LOOP_HANDOFF_RESULT_V1`` termination for a
cycle where a hard dependency was confirmed via any production lane
(trusted human context, trusted anchor, controlled reframe, or the #2406
confirmed-hard-predecessor route). Without this wiring, adding the
producer/materializer alone left the #2424 defect this Issue exists to fix
fully reproducible after merge (the choke point existed but nothing in the
real termination path ever called it). See
``references/termination-policy.md`` (Dependency Materialization Gate) for
the mandatory Step 5 invocation contract.
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
from pathlib import Path
from typing import Any, Callable

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
EDIT_ISSUE_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "edit-issue" / "scripts"
# Stage 2-6 (Issue #36): the trusted repository slug comes from repo_identity.py only
# (config/repository_identity.v1.json); this module holds no slug constant of its own.
AGENT_GUARDS_IMPORT_ROOT = REPO_ROOT / "scripts" / "agent-guards"

RESULT_SCHEMA = "DEPENDENCY_MATERIALIZATION_RESULT_V1"
GATE_RESULT_SCHEMA = "TERMINATION_DEPENDENCY_GATE_RESULT_V1"

GATE_DECISIONS = frozenset({"proceed", "block_retryable", "block_persistent"})

# AC8 failure classes that a Step 5 termination gate caller should treat as
# transient/environment noise (bounded-retry, never escalate to a human) as
# opposed to a genuine persistent block (readback drift, capability that is
# fundamentally unavailable, a real semantic-human-judgment signal, or a
# detected body-only false-green).
_RETRYABLE_FAILURE_CLASSES = frozenset({"auth-or-environment-failure", "controlled-executor-failure"})

FAILURE_CLASSES = frozenset(
    {
        "native-capability-unavailable",
        "controlled-executor-failure",
        "auth-or-environment-failure",
        "readback-mismatch",
        "semantic-human-judgment-required",
    }
)

# Error codes raised deep inside edit_issue_txn.py's native-relationship
# phases (Phase A/B/C), mapped to the AC8 failure-class taxonomy. Kept as a
# plain data table (not re-derived from edit_issue_txn.py) because these are
# stable, documented contract codes, not schema field-name literals -- AC10
# only forbids a duplicate *mutation surface*, not describing the existing
# one's documented error vocabulary.
_FAILURE_CLASS_BY_ERROR_CODE: dict[str, str] = {
    "relationship_pre_readback_failed": "auth-or-environment-failure",
    "post_relationship_readback_failed": "auth-or-environment-failure",
    "expected_before_drift_detected": "readback-mismatch",
    "final_native_relationship_readback_drift": "readback-mismatch",
    "concurrent_content_drift_after_relationship_mutation": "readback-mismatch",
    "graph_invariant_violation": "semantic-human-judgment-required",
    "readiness_forwarding_requires_human_judgment": "semantic-human-judgment-required",
    "issue_relationship_update_failed": "controlled-executor-failure",
    "issue_relationship_update_child_receipt_lost": "controlled-executor-failure",
}


def _require_trusted_repo(repo: object) -> None:
    """Fail closed (before ANY gh read or write) unless ``repo`` is the trusted slug.

    repo_identity is loaded from the fixed on-disk path on every call (no sys.path /
    sys.modules lookup), so a shadowing module can never answer for the trusted slug. An
    unusable identity config/module is a rejection, never a skip.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_dependency_materializer_repo_identity", AGENT_GUARDS_IMPORT_ROOT / "repo_identity.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("repo_identity_spec_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        trusted = module.is_trusted_repo(repo)
    except Exception as exc:
        raise ValueError("repo_identity_unavailable") from exc
    if not trusted:
        raise ValueError("repo_untrusted")


def _load_edit_issue_txn():
    """Deferred import of edit_issue_txn.py (Issue #2435 AC10): this module
    never re-implements a GitHub relationship read or write -- it only
    forwards a computed delta to the existing controlled-executor entry
    point. Deferred so pure-function callers (producer-only usage, unit
    tests of the delta math) never need edit_issue_txn.py's own optional
    dependency surface."""
    if str(EDIT_ISSUE_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(EDIT_ISSUE_SCRIPTS_DIR))
    import edit_issue_txn  # type: ignore

    return edit_issue_txn


def _resolve_native_predecessor_delta_keys(edit_txn_module: Any) -> tuple[str, str]:
    """Resolve the explicit add-side / remove-side blocked-by field names
    directly from edit_issue_txn.py's own ``NATIVE_RELATIONSHIPS_KEYS``
    constant, instead of re-declaring a second literal copy of those field
    names in this file (Issue #2435 AC10 -- edit_issue_txn.py stays the
    single place that spells out the native_relationships schema surface).
    """
    keys = edit_txn_module.NATIVE_RELATIONSHIPS_KEYS
    add_matches = [k for k in keys if k.startswith("add") and "block" in k and "blocking" not in k]
    remove_matches = [k for k in keys if k.startswith("remove") and "block" in k and "blocking" not in k]
    if len(add_matches) != 1 or len(remove_matches) != 1:
        raise RuntimeError("unable to resolve a unique native predecessor add/remove key pair")
    return add_matches[0], remove_matches[0]


# ---------------------------------------------------------------------------
# Producer: ISSUE_EXECUTION_DECISION_V1 -> desired native predecessor set
# ---------------------------------------------------------------------------


def derive_desired_predecessors(execution_decision: dict[str, Any] | None, target_issue_number: int) -> list[int]:
    """Project a confirmed ISSUE_EXECUTION_DECISION_V1 decision into the
    desired native-predecessor set for ``target_issue_number`` (AC2). Reads
    ``relations[].relation_type == "depends_on"`` edges whose source is the
    target, unioned with ``execution.predecessors[]``. Never fabricates a
    relation this module did not already receive from a confirmed decision.
    """
    if not isinstance(execution_decision, dict):
        return []
    relations = execution_decision.get("relations")
    from_relations: set[int] = set()
    if isinstance(relations, list):
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            if relation.get("source_issue_number") != target_issue_number:
                continue
            if relation.get("relation_type") != "depends_on":
                continue
            target = relation.get("target_issue_number")
            if isinstance(target, int) and not isinstance(target, bool):
                from_relations.add(target)

    execution = execution_decision.get("execution")
    from_predecessors: set[int] = set()
    if isinstance(execution, dict):
        predecessors = execution.get("predecessors")
        if isinstance(predecessors, list):
            from_predecessors = {p for p in predecessors if isinstance(p, int) and not isinstance(p, bool)}

    return sorted(from_relations | from_predecessors)


def derive_stale_predecessors(
    previous_execution_decision: dict[str, Any] | None,
    current_execution_decision: dict[str, Any] | None,
    target_issue_number: int,
) -> list[int]:
    """Explicit *decision* delta (AC3/AC7(b)): predecessors a previously
    confirmed ISSUE_EXECUTION_DECISION_V1 snapshot asserted for
    ``target_issue_number`` that the current confirmed snapshot no longer
    asserts. This is deliberately never computed against live GitHub state
    -- only against two confirmed decision snapshots this pipeline itself
    produced -- so a foreign native predecessor relation nobody in this
    pipeline ever confirmed can never appear here and can never be removed.
    """
    if not isinstance(previous_execution_decision, dict):
        return []
    previous = set(derive_desired_predecessors(previous_execution_decision, target_issue_number))
    current = set(derive_desired_predecessors(current_execution_decision, target_issue_number))
    return sorted(previous - current)


# ---------------------------------------------------------------------------
# Delta / postcondition math (pure)
# ---------------------------------------------------------------------------


def compute_add_and_remove_targets(
    desired_predecessors: list[int],
    stale_predecessors_to_remove: list[int],
    live_predecessors_before: list[int],
) -> tuple[list[int], list[int]]:
    """Compute the explicit add / remove delta to hand to the controlled
    executor (AC3). ``remove`` is bounded to ``stale_predecessors_to_remove``
    (an explicit instruction, never "everything live but not desired") and
    further bounded to what is actually live (removing something already
    absent is a no-op, not an instruction). ``add`` is whatever is desired
    but not yet live, excluding anything simultaneously marked for removal.
    """
    live_set = set(live_predecessors_before)
    remove_targets = sorted(set(stale_predecessors_to_remove) & live_set)
    add_targets = sorted((set(desired_predecessors) - live_set) - set(remove_targets))
    return add_targets, remove_targets


def compute_expected_predecessors_after(
    live_predecessors_before: list[int], add_targets: list[int], remove_targets: list[int]
) -> list[int]:
    """Postcondition only (AC3/AC4): ``(live_before - remove) | add``. Never
    used to derive the mutation instruction itself -- only to verify, via an
    independent fresh readback, that materialization produced exactly the
    expected native predecessor set (including preserving any unrelated
    pre-existing predecessor untouched by ``remove_targets``)."""
    return sorted((set(live_predecessors_before) - set(remove_targets)) | set(add_targets))


# ---------------------------------------------------------------------------
# Failure classification (AC8)
# ---------------------------------------------------------------------------


def classify_materialization_failure(
    *, error_code: str | None = None, readiness_status: str | None = None
) -> str | None:
    """Classify a materialization failure signal into the AC8 taxonomy.
    Returns ``None`` when there is no failure signal (success case)."""
    if readiness_status in ("human_judgment", "input_or_runtime_error"):
        return "semantic-human-judgment-required"
    if error_code is None:
        return None
    return _FAILURE_CLASS_BY_ERROR_CODE.get(error_code, "controlled-executor-failure")


def _first_error_code(errors: list[Any]) -> str | None:
    for item in errors:
        if isinstance(item, dict):
            code = item.get("code")
            if isinstance(code, str):
                return code
    return None


# ---------------------------------------------------------------------------
# Body-only false-green detector (AC5)
# ---------------------------------------------------------------------------

_BLOCKED_BY_HEADING_RE = re.compile(r"^##\s*Blocked By\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)
_ISSUE_REFERENCE_RE = re.compile(r"#(\d+)")


def extract_body_declared_predecessors(body_text: str) -> list[int]:
    """Best-effort extraction of issue numbers listed under a Markdown
    ``## Blocked By`` heading -- used only to detect the #2424-style
    body-only false-green (AC5), never as a source of truth for the desired
    predecessor set (that is always ``derive_desired_predecessors``)."""
    if not body_text:
        return []
    match = _BLOCKED_BY_HEADING_RE.search(body_text)
    if not match:
        return []
    remainder = body_text[match.end() :]
    next_heading = _NEXT_HEADING_RE.search(remainder)
    section_text = remainder[: next_heading.start()] if next_heading else remainder
    return sorted({int(n) for n in _ISSUE_REFERENCE_RE.findall(section_text)})


def detect_body_only_false_green(
    body_text: str, *, native_relationship_attempted: bool, capability_available: bool
) -> tuple[bool, str | None]:
    """AC5: if the body declares predecessors under ``## Blocked By`` while
    native capability was available but no native materialization was even
    attempted, this must fail closed -- the exact #2424 incident shape."""
    declared = extract_body_declared_predecessors(body_text)
    if declared and capability_available and not native_relationship_attempted:
        return True, "body_only_predecessor_mutation_without_native_materialization"
    return False, None


# ---------------------------------------------------------------------------
# Materializer (delegates every remote read/write to edit_issue_txn.py)
# ---------------------------------------------------------------------------


def _default_capability_preflight() -> tuple[bool, str]:
    edit_txn = _load_edit_issue_txn()
    return edit_txn._relationship_capability_preflight()


def _default_fetch_live_snapshot(issue_number: int, repo: str) -> tuple[dict[str, Any] | None, str]:
    edit_txn = _load_edit_issue_txn()
    return edit_txn._fetch_native_relationships(issue_number, repo)


def _default_fetch_issue_content(issue_number: int, repo: str) -> tuple[dict[str, Any] | None, str]:
    edit_txn = _load_edit_issue_txn()
    return edit_txn._fetch_issue(issue_number, repo)


def _write_repo_relative_tmp(relative_dir: str, suffix: str, text: str) -> str:
    directory = REPO_ROOT / relative_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / f"{stamp}-{id(text) & 0xFFFFFF:06x}{suffix}"
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def _default_readiness_result(body_text: str, issue_number: int) -> dict[str, Any]:
    """Fallback ``readiness_result`` used only when the caller does not pass
    one already computed by the existing workflow (P1 fix, OWNER review on
    PR #2447). This module never itself shells out to
    ``contract_readiness_check.py`` -- doing so previously converted a
    routine subprocess crash/non-JSON-output failure into a fabricated
    ``status: input_or_runtime_error`` signal that ``edit_issue_txn.py``
    then folded into an unnecessary ``human_judgment`` escalation. The
    production caller (the Step 5 termination gate) always has an
    already-confirmed ``status: go`` readiness available -- the Final Gate
    (``references/termination-policy.md``) requires
    ``CONTRACT_REVIEW_RESULT_V1.status == "go"`` before an ``approved``
    termination is even reachable -- so this default exists only to keep
    this module usable by isolated callers/tests that have no separate
    readiness pipeline of their own; it performs no check of its own."""
    body_sha256 = "sha256:" + hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    parsed = {"status": "go", "body_sha256": body_sha256, "source_checks": [], "errors": []}
    readiness_ref = _write_repo_relative_tmp(
        f"artifacts/{issue_number}/dependency-materialization", ".readiness.json", json.dumps(parsed)
    )
    return {**parsed, "readiness_result_ref": readiness_ref}


def _default_invoke_edit_issue_txn(input_payload: dict[str, Any], *, issue_number: int) -> dict[str, Any]:
    edit_txn = _load_edit_issue_txn()
    input_ref = _write_repo_relative_tmp(
        f"artifacts/{issue_number}/dependency-materialization", ".edit_txn_input.json", json.dumps(input_payload)
    )
    cp = subprocess.run(
        [sys.executable, str(edit_txn.SCRIPT_PATH), "--input-file", input_ref],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {
            "schema": "ISSUE_EDIT_TXN_RESULT_V1",
            "status": "failed_no_mutation",
            "native_relationships": {"attempted": False, "status": "not_run", "errors": []},
            "errors": [{"code": "edit_issue_txn_non_json_output", "message": (cp.stderr or cp.stdout)[:400]}],
        }


def _render_materialization_result(
    *,
    status: str,
    target_issue_number: int,
    repo: str,
    desired_predecessors: list[int],
    stale_predecessors_to_remove: list[int],
    live_predecessors_before: list[int] | None,
    expected_predecessors_after: list[int] | None,
    observed_predecessors_after: list[int] | None,
    native_relationship_materialized: bool,
    failure_class: str | None,
    edit_txn_status: str | None,
    edit_txn_result_ref: str | None = None,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        # AC11: this field asserts only that the desired native predecessor
        # SET was materialized and independently confirmed via readback. It
        # is NOT an implementation-readiness / "is a predecessor still open"
        # judgment -- that responsibility belongs to #265 and is never
        # duplicated here.
        "native_relationship_materialized": native_relationship_materialized,
        "failure_class": failure_class,
        "target_issue_number": target_issue_number,
        "repo": repo,
        "desired_predecessors": sorted(desired_predecessors),
        "stale_predecessors_to_remove": sorted(stale_predecessors_to_remove),
        "live_predecessors_before": live_predecessors_before,
        "expected_predecessors_after": expected_predecessors_after,
        "observed_predecessors_after": observed_predecessors_after,
        "edit_txn_status": edit_txn_status,
        "edit_txn_result_ref": edit_txn_result_ref,
        "errors": errors or [],
    }


def materialize_dependencies(
    *,
    target_issue_number: int,
    repo: str,
    desired_predecessors: list[int],
    stale_predecessors_to_remove: list[int] | None = None,
    readiness_result: dict[str, Any] | None = None,
    capability_preflight: Callable[[], tuple[bool, str]] = _default_capability_preflight,
    fetch_live_snapshot: Callable[[int, str], tuple[dict[str, Any] | None, str]] = _default_fetch_live_snapshot,
    fetch_issue_content: Callable[[int, str], tuple[dict[str, Any] | None, str]] = _default_fetch_issue_content,
    invoke_edit_issue_txn: Callable[..., dict[str, Any]] = _default_invoke_edit_issue_txn,
) -> dict[str, Any]:
    """The single common dependency-materialization choke point (AC2/AC9).
    Every collaborator is injectable so this function can be exercised
    deterministically (tests monkeypatch the seams) without ever making a
    live GitHub call -- and so any other lane (#2406's confirmed-hard-
    predecessor route included) can reuse this exact function unmodified.

    ``readiness_result`` (P1 fix, OWNER review on PR #2447): the
    already-computed ``readiness_forwarding_payload.readiness_result`` the
    calling workflow produced earlier in the same cycle. This module never
    invokes ``contract_readiness_check.py`` itself -- if the caller omits
    this, a no-op ``status: go`` default is used (see
    ``_default_readiness_result``); it is never derived from an internal
    subprocess call.
    """
    desired = sorted(set(desired_predecessors))
    stale = sorted(set(stale_predecessors_to_remove or []))

    try:
        _require_trusted_repo(repo)
    except ValueError as exc:
        # Persistent block (never retryable): rejected before the capability preflight, the
        # live snapshot read and the edit transaction, so a wrong repo makes zero gh calls.
        return _render_materialization_result(
            status="blocked",
            target_issue_number=target_issue_number,
            repo=repo if isinstance(repo, str) else "",
            desired_predecessors=desired,
            stale_predecessors_to_remove=stale,
            live_predecessors_before=None,
            expected_predecessors_after=None,
            observed_predecessors_after=None,
            native_relationship_materialized=False,
            failure_class="semantic-human-judgment-required",
            edit_txn_status=None,
            errors=[{"code": str(exc), "message": "repo is not the trusted repository identity"}],
        )

    cap_ok, cap_err = capability_preflight()
    if not cap_ok:
        failure_class = (
            "native-capability-unavailable"
            if cap_err == "gh_binary_not_found"
            else "auth-or-environment-failure"
        )
        return _render_materialization_result(
            status="blocked",
            target_issue_number=target_issue_number,
            repo=repo,
            desired_predecessors=desired,
            stale_predecessors_to_remove=stale,
            live_predecessors_before=None,
            expected_predecessors_after=None,
            observed_predecessors_after=None,
            native_relationship_materialized=False,
            failure_class=failure_class,
            edit_txn_status=None,
            errors=[{"code": "relationship_capability_preflight_failed", "message": cap_err}],
        )

    live_snapshot, live_err = fetch_live_snapshot(target_issue_number, repo)
    if live_snapshot is None:
        return _render_materialization_result(
            status="blocked",
            target_issue_number=target_issue_number,
            repo=repo,
            desired_predecessors=desired,
            stale_predecessors_to_remove=stale,
            live_predecessors_before=None,
            expected_predecessors_after=None,
            observed_predecessors_after=None,
            native_relationship_materialized=False,
            failure_class=classify_materialization_failure(error_code="relationship_pre_readback_failed"),
            edit_txn_status=None,
            errors=[{"code": "relationship_pre_readback_failed", "message": live_err}],
        )

    live_predecessors_before = sorted(live_snapshot.get("blocked_by", []))
    add_targets, remove_targets = compute_add_and_remove_targets(desired, stale, live_predecessors_before)
    expected_after = compute_expected_predecessors_after(live_predecessors_before, add_targets, remove_targets)

    if not add_targets and not remove_targets:
        # Nothing to materialize: the live set already matches the desired
        # instruction. No GitHub write is issued (AC3 -- only an explicit
        # delta ever triggers a mutation).
        return _render_materialization_result(
            status="ok",
            target_issue_number=target_issue_number,
            repo=repo,
            desired_predecessors=desired,
            stale_predecessors_to_remove=stale,
            live_predecessors_before=live_predecessors_before,
            expected_predecessors_after=expected_after,
            observed_predecessors_after=live_predecessors_before,
            native_relationship_materialized=True,
            failure_class=None,
            edit_txn_status="no_op_not_attempted",
            errors=[],
        )

    issue_content, issue_err = fetch_issue_content(target_issue_number, repo)
    if issue_content is None:
        return _render_materialization_result(
            status="blocked",
            target_issue_number=target_issue_number,
            repo=repo,
            desired_predecessors=desired,
            stale_predecessors_to_remove=stale,
            live_predecessors_before=live_predecessors_before,
            expected_predecessors_after=expected_after,
            observed_predecessors_after=None,
            native_relationship_materialized=False,
            failure_class=classify_materialization_failure(error_code="post_relationship_readback_failed"),
            edit_txn_status=None,
            errors=[{"code": "issue_content_readback_failed", "message": issue_err}],
        )

    current_body = issue_content.get("body", "")
    current_updated_at = issue_content.get("updatedAt", "")
    if readiness_result is not None:
        effective_readiness_result = readiness_result
    else:
        effective_readiness_result = _default_readiness_result(current_body, target_issue_number)

    edit_txn = _load_edit_issue_txn()
    add_key, remove_key = _resolve_native_predecessor_delta_keys(edit_txn)
    native_relationships_block: dict[str, Any] = {
        "expected_before": {
            "parent": live_snapshot.get("parent"),
            "blocked_by": live_predecessors_before,
            "blocking": sorted(live_snapshot.get("blocking", [])),
        },
        "parent": {"action": "unchanged", "issue_number": None},
        add_key: add_targets,
        remove_key: remove_targets,
        "add_blocking": [],
        "remove_blocking": [],
    }

    new_body_ref = _write_repo_relative_tmp("tmp", ".md", current_body)
    edit_txn_input = {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": target_issue_number,
        "repo": repo,
        "new_body_file": new_body_ref,
        "readiness_forwarding_payload": {"readiness_result": effective_readiness_result},
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": "sha256:" + hashlib.sha256(current_body.encode("utf-8")).hexdigest(),
        "expected_previous_updated_at": current_updated_at,
        "title_update": {"required": False, "proposed_title": None, "reason": None},
        "native_relationships": native_relationships_block,
    }

    edit_txn_result = invoke_edit_issue_txn(edit_txn_input, issue_number=target_issue_number)
    edit_txn_status = edit_txn_result.get("status") if isinstance(edit_txn_result, dict) else None
    native_relationships_result = (
        edit_txn_result.get("native_relationships", {}) if isinstance(edit_txn_result, dict) else {}
    )
    observed_after_raw = native_relationships_result.get("after")
    observed_after = (
        sorted((observed_after_raw or {}).get("blocked_by", []))
        if isinstance(observed_after_raw, dict)
        else None
    )

    all_errors = list(native_relationships_result.get("errors", []) or []) + list(
        (edit_txn_result or {}).get("errors", []) or []
    )
    error_code = _first_error_code(all_errors)

    if edit_txn_status in ("ok",) and observed_after is not None and observed_after == expected_after:
        return _render_materialization_result(
            status="ok",
            target_issue_number=target_issue_number,
            repo=repo,
            desired_predecessors=desired,
            stale_predecessors_to_remove=stale,
            live_predecessors_before=live_predecessors_before,
            expected_predecessors_after=expected_after,
            observed_predecessors_after=observed_after,
            native_relationship_materialized=True,
            failure_class=None,
            edit_txn_status=edit_txn_status,
            errors=[],
        )

    if edit_txn_status in ("ok",) and observed_after is not None and observed_after != expected_after:
        # AC4: an executor self-reported success must still be independently
        # re-verified. A mismatch here is a readback-mismatch, never "ok".
        failure_class = "readback-mismatch"
    else:
        failure_class = classify_materialization_failure(
            error_code=error_code, readiness_status=edit_txn_status if edit_txn_status == "human_judgment" else None
        )

    return _render_materialization_result(
        status="human_judgment" if failure_class == "semantic-human-judgment-required" else "failed",
        target_issue_number=target_issue_number,
        repo=repo,
        desired_predecessors=desired,
        stale_predecessors_to_remove=stale,
        live_predecessors_before=live_predecessors_before,
        expected_predecessors_after=expected_after,
        observed_predecessors_after=observed_after,
        native_relationship_materialized=False,
        failure_class=failure_class,
        edit_txn_status=edit_txn_status,
        errors=all_errors or [{"code": "dependency_materialization_failed", "message": "see edit_txn_status"}],
    )


# ---------------------------------------------------------------------------
# Step 5 termination gate (P0, OWNER review on PR #2447)
# ---------------------------------------------------------------------------


def _render_gate_result(
    *,
    decision: str,
    reason: str,
    materialization_result: dict[str, Any] | None,
    body_only_false_green: bool,
    body_only_reason: str | None,
) -> dict[str, Any]:
    return {
        "schema": GATE_RESULT_SCHEMA,
        "decision": decision,
        "reason": reason,
        "materialization_result": materialization_result,
        "body_only_false_green": body_only_false_green,
        "body_only_reason": body_only_reason,
    }


def evaluate_termination_dependency_gate(
    *,
    target_issue_number: int,
    repo: str,
    current_execution_decision: dict[str, Any] | None,
    previous_execution_decision: dict[str, Any] | None = None,
    current_body: str = "",
    materialize: Callable[..., dict[str, Any]] = materialize_dependencies,
) -> dict[str, Any]:
    """The mandatory Step 5 termination gate (Issue #2435 P0, OWNER review on
    PR #2447): ``issue-refinement-loop`` MUST call this before posting an
    ``approved`` / ``LOOP_HANDOFF_RESULT_V1`` termination for a cycle where a
    hard dependency was confirmed via any production lane (trusted human
    context, trusted anchor, controlled reframe, or the #2406
    confirmed-hard-predecessor route). See
    ``references/termination-policy.md`` (Dependency Materialization Gate)
    for the full Step 5 invocation contract.

    Projects ``current_execution_decision`` (this cycle's confirmed
    ``ISSUE_EXECUTION_DECISION_V1``) via ``derive_desired_predecessors``,
    calls ``materialize_dependencies``, and folds the body-only
    false-green detector (AC5) into the same decision so a caller cannot
    reach ``proceed`` by posting a body-only ``## Blocked By`` mutation
    while native materialization was never even attempted.

    Returns ``TERMINATION_DEPENDENCY_GATE_RESULT_V1``:

    - ``decision: proceed`` -- either no hard dependency was confirmed this
      cycle (no-op; a cycle with no confirmed dependency is never required
      to have one), or materialization succeeded and no body-only
      false-green was detected. The orchestrator may continue with the
      ``approved`` termination.
    - ``decision: block_retryable`` -- a transient auth/environment or
      controlled-executor failure (``_RETRYABLE_FAILURE_CLASSES``). The
      orchestrator should bounded-retry, never escalate to a human on this
      alone.
    - ``decision: block_persistent`` -- a readback mismatch, a genuine
      ``semantic-human-judgment-required`` signal, native capability being
      fundamentally unavailable, or a detected body-only false-green
      (AC5). The orchestrator MUST NOT post the ``approved`` termination
      until this is resolved.
    """
    desired = derive_desired_predecessors(current_execution_decision, target_issue_number)
    stale = derive_stale_predecessors(previous_execution_decision, current_execution_decision, target_issue_number)

    if not desired and not stale:
        return _render_gate_result(
            decision="proceed",
            reason="no_hard_dependencies_confirmed_this_cycle",
            materialization_result=None,
            body_only_false_green=False,
            body_only_reason=None,
        )

    materialization_result = materialize(
        target_issue_number=target_issue_number,
        repo=repo,
        desired_predecessors=desired,
        stale_predecessors_to_remove=stale,
    )

    failure_class = materialization_result.get("failure_class")
    capability_available = failure_class != "native-capability-unavailable"
    native_relationship_attempted = materialization_result.get("edit_txn_status") not in (
        None,
        "no_op_not_attempted",
    )
    body_only, body_only_reason = detect_body_only_false_green(
        current_body,
        native_relationship_attempted=native_relationship_attempted,
        capability_available=capability_available,
    )

    if body_only:
        # AC5, wired end-to-end: a body-only false-green always blocks the
        # approved termination, independent of the underlying failure_class
        # shape (this is the exact #2424 incident silhouette -- body
        # declares predecessors, native materialization was never even
        # attempted, capability was available).
        return _render_gate_result(
            decision="block_persistent",
            reason="body_only_false_green_detected",
            materialization_result=materialization_result,
            body_only_false_green=True,
            body_only_reason=body_only_reason,
        )

    if materialization_result.get("status") == "ok":
        return _render_gate_result(
            decision="proceed",
            reason="native_relationship_materialized",
            materialization_result=materialization_result,
            body_only_false_green=False,
            body_only_reason=None,
        )

    if failure_class in _RETRYABLE_FAILURE_CLASSES:
        return _render_gate_result(
            decision="block_retryable",
            reason=f"materialization_failure:{failure_class}",
            materialization_result=materialization_result,
            body_only_false_green=False,
            body_only_reason=None,
        )

    return _render_gate_result(
        decision="block_persistent",
        reason=f"materialization_failure:{failure_class or 'unknown'}",
        materialization_result=materialization_result,
        body_only_false_green=False,
        body_only_reason=None,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_int_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _read_json_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_materialize(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Materialize an explicit add/remove native predecessor delta.")
    parser.add_argument("--target-issue", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--desired-predecessors", default="", help="comma-separated issue numbers")
    parser.add_argument("--stale-predecessors", default="", help="comma-separated issue numbers to explicitly remove")
    parser.add_argument(
        "--readiness-result-file",
        default=None,
        help=(
            "path to an already-computed readiness_result JSON produced earlier in this cycle "
            "(P1 fix: this module never re-runs contract_readiness_check.py itself)"
        ),
    )
    args = parser.parse_args(argv)

    result = materialize_dependencies(
        target_issue_number=args.target_issue,
        repo=args.repo,
        desired_predecessors=_parse_int_list(args.desired_predecessors),
        stale_predecessors_to_remove=_parse_int_list(args.stale_predecessors),
        readiness_result=_read_json_file(args.readiness_result_file),
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 1


def _cmd_gate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 5 termination gate (Issue #2435 P0): evaluate whether an approved "
            "termination may proceed for this cycle's confirmed hard dependency."
        )
    )
    parser.add_argument("--target-issue", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--current-decision-file",
        required=True,
        help="path to this cycle's confirmed ISSUE_EXECUTION_DECISION_V1 JSON",
    )
    parser.add_argument(
        "--previous-decision-file",
        default=None,
        help="path to the prior cycle's confirmed ISSUE_EXECUTION_DECISION_V1 JSON, if any",
    )
    parser.add_argument("--body-file", default=None, help="path to the current Issue body text")
    args = parser.parse_args(argv)

    current_decision = _read_json_file(args.current_decision_file)
    previous_decision = _read_json_file(args.previous_decision_file)
    body_text = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""

    result = evaluate_termination_dependency_gate(
        target_issue_number=args.target_issue,
        repo=args.repo,
        current_execution_decision=current_decision,
        previous_execution_decision=previous_decision,
        current_body=body_text,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    if result["decision"] == "proceed":
        return 0
    if result["decision"] == "block_retryable":
        return 2
    return 1


def main(argv: list[str] | None = None) -> int:
    """Dispatches to the ``gate`` (Step 5 termination gate) or
    ``materialize`` (standalone delta materialization) subcommand. No
    subcommand token is treated as ``materialize`` for backward
    compatibility with the original Issue #2435 CLI contract."""
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "gate":
        return _cmd_gate(raw_argv[1:])
    if raw_argv and raw_argv[0] == "materialize":
        return _cmd_materialize(raw_argv[1:])
    return _cmd_materialize(raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
