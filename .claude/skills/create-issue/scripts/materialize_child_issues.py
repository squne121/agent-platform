#!/usr/bin/env python3
"""Materialize delivery-rollup child issues through the create_issue_txn canonical flow.

This consumer reads a ``CHILD_MATERIALIZATION_PLAN_V2``-compatible plan (closed schema),
renders canonical issue bodies from ``ISSUE_TEMPLATE/<kind>.yml`` required label order,
validates them via ``validate_issue_body.py``, creates issues via ``create_issue_txn.py``
(never raw ``gh issue create``), maps ``depends_on`` to GitHub dependencies with
read-back, safely patches the parent ``## Child Issues`` checklist, and emits a
``CHILD_MATERIALIZATION_RESULT_V2`` JSON document.

Design boundaries (see docs/dev/agent-skill-boundaries.md#child-materialization-executor):

* Issue **creation** flows ONLY through ``create_issue_txn.py``. The materializer never
  spawns ``gh issue create`` directly (AC6). The label profile (``standard`` /
  ``triage_only``) is forwarded to the transaction helper, not to a raw ``gh`` call.
* Parent checklist **patch** is the only direct ``gh`` mutation this module performs, and
  it is gated by a REQUIRED ``parent.body_sha256`` match + exact ``old_line`` +
  ``expected_match_count == 1`` + post-edit read-back (AC5). It is applied only when the
  overall result is ``ok`` with no errors and no escalation items (AC7).
* The plan is validated against a closed schema. Any unknown key, duplicate ``child_id``,
  ``issue_lookup.complete: false``, invalid ``action``, non-integer ``depends_on``, empty
  ``allowed_paths``, AC/VC set mismatch, control-char injection, or missing
  ``body_sha256`` (when patching) is fail-closed (AC1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import importlib.util
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

# validate_issue_body lives next to this script; reuse its template loader as a cross-check
# but the materializer uses a STRICT loader (fail-closed on missing/malformed template).
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

# Repo root: .../.claude/skills/create-issue/scripts -> parents[3]
_REPO_ROOT = _SCRIPT_DIR.parents[3]
_ISSUE_TEMPLATE_DIR = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE"

# Stage 2-6 (Issue #36): the trusted repository slug comes from repo_identity.py only
# (config/repository_identity.v1.json); this module holds no slug constant of its own.
_AGENT_GUARDS_ROOT = _REPO_ROOT / "scripts" / "agent-guards"


def _require_trusted_repo(repo: object) -> None:
    """Fail closed (before ANY gh / runner call) unless ``repo`` is the trusted slug.

    repo_identity is loaded from the fixed on-disk path on every call (no sys.path /
    sys.modules lookup) so a shadowing module cannot answer for the trusted slug. An
    unusable identity config/module is a rejection, never a skip.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_materialize_child_issues_repo_identity", _AGENT_GUARDS_ROOT / "repo_identity.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("repo_identity_spec_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        trusted = module.is_trusted_repo(repo)
    except Exception as exc:
        raise PlanValidationError("repo_identity_unavailable") from exc
    if not trusted:
        raise PlanValidationError("repo_untrusted")


# --------------------------------------------------------------------------------------
# Closed schema (AC1)
# --------------------------------------------------------------------------------------

SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Disallow control chars / fence break-out / backtick injection in rendered fields.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_ALLOWED_TOP_KEYS = {
    "schema_version",
    "repo",
    "generated_at",
    "source",
    "parent",
    "issue_lookup",
    "children",
    "parent_body_updates",
    "overlap",
    "warnings",
}

_ALLOWED_PARENT_KEYS = {"issue_number", "parent_mode", "closure_mode", "body_sha256"}

_ALLOWED_CHILD_KEYS = {
    "child_id",
    "title",
    "kind",
    "action",
    "depends_on",
    "allowed_paths",
    "acceptance_criteria",
    "verification_commands",
    "sections",
    "label_profile",
    "dedupe_key",
    "status",
    "existing_issue",
}

_ALLOWED_ACTIONS = {
    "create_issue",
    "reuse_and_update_parent",
    "no_op",
    "human_escalation",
    "register_subissue_or_human_escalation",
}

_ALLOWED_KINDS = {"implementation", "research"}

_ALLOWED_LABEL_PROFILES = {"standard", "triage_only"}

_ALLOWED_PARENT_UPDATE_KEYS = {
    "section",
    "line_number",
    "old_line",
    "new_line",
    "expected_match_count",
    "body_sha256",
}

# overlap gate (AC8). status=clear must carry preflight provenance so a plan producer
# cannot assert "clear" from untrusted input alone (High 2).
_ALLOWED_OVERLAP_KEYS = {
    "status",
    "depends_on_issue",
    "reason",
    "source",
    "helper_version",
    "input_sha256",
    "checked_at",
    "verdict",
}
_ALLOWED_OVERLAP_STATUS = {"clear", "deferred_to_issue", "not_run", "undeterminable"}
_SAFE_OVERLAP_VERDICTS = {"safe_new_issue", "no_overlap"}

# Default follow-up issue that owns the overlap preflight helper (#948). When the overlap
# gate is deferred, every created child must declare this issue as a dependency (AC8).
DEFAULT_OVERLAP_ISSUE = 948


class PlanValidationError(Exception):
    """Raised when the input plan violates the closed schema (fail-closed, AC1)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanValidationError(message)


def _require_clean_text(value: str, where: str) -> None:
    _require(_CTRL_RE.search(value) is None, f"{where} contains control characters")


def validate_plan(raw: Any) -> dict[str, Any]:
    """Validate a CHILD_MATERIALIZATION_PLAN_V2-compatible plan against a closed schema.

    Returns the plan dict unchanged when valid. Raises :class:`PlanValidationError` for
    any schema violation. Non-dict / non-JSON input is rejected (no silent fallback).
    """
    _require(isinstance(raw, dict), "plan must be a JSON object")

    unknown = set(raw) - _ALLOWED_TOP_KEYS
    _require(not unknown, f"unknown top-level key(s): {sorted(unknown)}")

    _require(raw.get("schema_version") == SCHEMA_VERSION, "schema_version must be 2")
    _require(isinstance(raw.get("repo"), str) and raw["repo"], "repo must be a non-empty string")

    parent = raw.get("parent")
    _require(isinstance(parent, dict), "parent must be an object")
    unknown_parent = set(parent) - _ALLOWED_PARENT_KEYS
    _require(not unknown_parent, f"unknown parent key(s): {sorted(unknown_parent)}")
    _require(isinstance(parent.get("issue_number"), int), "parent.issue_number must be an integer")

    # issue_lookup.complete: false → consumer must NOT mutate (human escalation).
    lookup = raw.get("issue_lookup")
    if lookup is not None:
        _require(isinstance(lookup, dict), "issue_lookup must be an object")
        complete = lookup.get("complete", True)
        _require(isinstance(complete, bool), "issue_lookup.complete must be a boolean")
        _require(complete is True, "issue_lookup.complete is false: mutation forbidden")

    children = raw.get("children")
    _require(isinstance(children, list), "children must be a list")

    seen_ids: set[str] = set()
    for idx, child in enumerate(children):
        _validate_child(child, idx, seen_ids)

    overlap = raw.get("overlap")
    if overlap is not None:
        _validate_overlap(overlap)

    updates = raw.get("parent_body_updates", [])
    _require(isinstance(updates, list), "parent_body_updates must be a list")
    for u_idx, upd in enumerate(updates):
        _validate_parent_update(upd, u_idx)

    # Blocker 2: a parent checklist patch requires the producer to pin the parent body
    # hash, so a stale plan cannot mutate a drifted parent body.
    if updates:
        sha = parent.get("body_sha256")
        _require(
            isinstance(sha, str) and _SHA256_RE.match(sha) is not None,
            "parent.body_sha256 (sha256:<64 hex>) is required when parent_body_updates is non-empty",
        )

    return raw


def _validate_child(child: Any, idx: int, seen_ids: set[str]) -> None:
    _require(isinstance(child, dict), f"children[{idx}] must be an object")
    unknown = set(child) - _ALLOWED_CHILD_KEYS
    _require(not unknown, f"children[{idx}] unknown key(s): {sorted(unknown)}")

    child_id = child.get("child_id")
    _require(isinstance(child_id, str) and child_id, f"children[{idx}].child_id must be a non-empty string")
    _require(child_id not in seen_ids, f"duplicate child_id: {child_id!r}")
    seen_ids.add(child_id)

    action = child.get("action")
    _require(action in _ALLOWED_ACTIONS, f"children[{idx}] invalid action: {action!r}")

    depends_on = child.get("depends_on", [])
    _require(isinstance(depends_on, list), f"children[{idx}].depends_on must be a list")
    for dep in depends_on:
        # bool is a subclass of int; reject it explicitly so free-form/garbage deps fail.
        _require(
            isinstance(dep, int) and not isinstance(dep, bool),
            f"children[{idx}].depends_on must contain integers only (got {dep!r})",
        )

    label_profile = child.get("label_profile", "standard")
    _require(
        label_profile in _ALLOWED_LABEL_PROFILES,
        f"children[{idx}] invalid label_profile: {label_profile!r}",
    )

    # Content-bearing requirements apply to issues that will actually be created.
    if action == "create_issue":
        kind = child.get("kind")
        _require(kind in _ALLOWED_KINDS, f"children[{idx}] invalid kind: {kind!r}")
        title = child.get("title")
        _require(isinstance(title, str) and title.strip(), f"children[{idx}].title must be a non-empty string")
        _require_clean_text(title, f"children[{idx}].title")

        allowed_paths = child.get("allowed_paths")
        _require(
            isinstance(allowed_paths, list) and len(allowed_paths) > 0,
            f"children[{idx}].allowed_paths must be a non-empty list",
        )
        for p in allowed_paths:
            _require(
                isinstance(p, str) and p.strip(),
                f"children[{idx}].allowed_paths entries must be non-empty strings"
            )
            _require_clean_text(p, f"children[{idx}].allowed_paths entry")
            _require("`" not in p, f"children[{idx}].allowed_paths entry must not contain backticks")

        ac = child.get("acceptance_criteria")
        vc = child.get("verification_commands")
        _require(isinstance(ac, list) and len(ac) > 0, f"children[{idx}].acceptance_criteria must be a non-empty list")
        _require(
            isinstance(vc, dict) and len(vc) > 0,
            f"children[{idx}].verification_commands must be a non-empty object"
        )
        ac_set = set(ac)
        vc_set = set(vc)
        _require(len(ac_set) == len(ac), f"children[{idx}].acceptance_criteria contains duplicate AC ids")
        _require(
            ac_set == vc_set,
            f"children[{idx}] AC/VC mismatch: acceptance_criteria={sorted(ac_set)} "
            f"verification_commands={sorted(vc_set)}",
        )
        for ac_id, cmd in vc.items():
            _require(
                isinstance(cmd, str) and cmd.strip(),
                f"children[{idx}].verification_commands[{ac_id}] must be non-empty"
            )
            # Reject code-fence break-out so the rendered ```bash block cannot be escaped.
            _require("```" not in cmd, f"children[{idx}].verification_commands[{ac_id}] must not contain a code fence")
            _require_clean_text(cmd, f"children[{idx}].verification_commands[{ac_id}]")


def _validate_overlap(overlap: Any) -> None:
    _require(isinstance(overlap, dict), "overlap must be an object")
    unknown = set(overlap) - _ALLOWED_OVERLAP_KEYS
    _require(not unknown, f"overlap unknown key(s): {sorted(unknown)}")
    status = overlap.get("status")
    _require(status in _ALLOWED_OVERLAP_STATUS, f"overlap.status invalid: {status!r}")
    dep = overlap.get("depends_on_issue")
    if dep is not None:
        _require(isinstance(dep, int) and not isinstance(dep, bool), "overlap.depends_on_issue must be an integer")

    # High 2: a "clear" verdict must be backed by preflight provenance — the materializer
    # does not trust a bare {"status": "clear"} from an untrusted plan producer.
    if status == "clear":
        _require(
            isinstance(overlap.get("source"), str) and overlap["source"].strip(),
            "overlap.status=clear requires non-empty overlap.source (preflight provenance)",
        )
        _require(
            overlap.get("verdict") in _SAFE_OVERLAP_VERDICTS,
            f"overlap.status=clear requires overlap.verdict in {sorted(_SAFE_OVERLAP_VERDICTS)}",
        )
        _require(
            isinstance(overlap.get("checked_at"), str) and overlap["checked_at"].strip(),
            "overlap.status=clear requires overlap.checked_at",
        )
        ish = overlap.get("input_sha256")
        _require(
            isinstance(ish, str) and _SHA256_RE.match(ish) is not None,
            "overlap.status=clear requires overlap.input_sha256 (sha256:<64 hex>)",
        )


def _validate_parent_update(upd: Any, idx: int) -> None:
    _require(isinstance(upd, dict), f"parent_body_updates[{idx}] must be an object")
    unknown = set(upd) - _ALLOWED_PARENT_UPDATE_KEYS
    _require(not unknown, f"parent_body_updates[{idx}] unknown key(s): {sorted(unknown)}")
    _require(isinstance(upd.get("old_line"), str) and upd["old_line"], f"parent_body_updates[{idx}].old_line required")
    _require(isinstance(upd.get("new_line"), str) and upd["new_line"], f"parent_body_updates[{idx}].new_line required")
    emc = upd.get("expected_match_count", 1)
    _require(emc == 1, f"parent_body_updates[{idx}].expected_match_count must be 1")
    section = upd.get("section", "Child Issues")
    _require(section == "Child Issues", f"parent_body_updates[{idx}].section must be 'Child Issues'")


# --------------------------------------------------------------------------------------
# Overlap gate (AC8)
# --------------------------------------------------------------------------------------

@dataclass
class OverlapGateResult:
    ok: bool
    escalations: list[dict] = field(default_factory=list)
    required_dependency: Optional[int] = None


def evaluate_overlap_gate(plan: dict[str, Any]) -> OverlapGateResult:
    """Decide whether child creation may proceed under the overlap preflight policy (AC8)."""
    overlap = plan.get("overlap") or {"status": "not_run"}
    status = overlap.get("status", "not_run")
    dep_issue = overlap.get("depends_on_issue", DEFAULT_OVERLAP_ISSUE)

    if status == "clear":
        # provenance already enforced by _validate_overlap
        return OverlapGateResult(ok=True)

    if status == "undeterminable":
        return OverlapGateResult(
            ok=False,
            escalations=[
                {
                    "child_id": "*",
                    "reason": overlap.get("reason")
                    or "Allowed Paths overlap is undeterminable; routing to human_escalation",
                }
            ],
        )

    # not_run / deferred_to_issue: require the #948 dependency on every created child.
    escalations: list[dict] = []
    for child in plan.get("children", []):
        if child.get("action") != "create_issue":
            continue
        deps = child.get("depends_on", [])
        if dep_issue not in deps:
            escalations.append(
                {
                    "child_id": child.get("child_id"),
                    "reason": (
                        f"overlap preflight not run; child must declare #{dep_issue} as a "
                        f"dependency or wait for the overlap helper"
                    ),
                }
            )
    return OverlapGateResult(ok=not escalations, escalations=escalations, required_dependency=dep_issue)


# --------------------------------------------------------------------------------------
# Canonical body rendering (AC2)
# --------------------------------------------------------------------------------------

_DEFAULT_SECTION_TEXT = {
    "Remaining Parent Gaps": "なし",
    "Out of Scope": "- 本 child のスコープ外の変更",
    "Required Skills": "なし",
    "Scope Delta（任意）": "N/A",
    "Required Design References": "- docs/dev/agent-skill-boundaries.md",
}


def required_section_labels(kind: str) -> list[str]:
    """Return the required section labels for ``kind`` in template order (spec-driven).

    Medium 2: the materializer uses a STRICT loader. Unlike the validator's
    backward-compatible loader, a missing / malformed template or an empty required-label
    set is fail-closed — the materializer must not silently downgrade the canonical body.
    """
    template_path = _ISSUE_TEMPLATE_DIR / f"{kind}.yml"
    if not template_path.exists():
        raise PlanValidationError(f"ISSUE_TEMPLATE/{kind}.yml not found; cannot render canonical body")
    try:
        form = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise PlanValidationError(f"ISSUE_TEMPLATE/{kind}.yml is malformed: {exc}") from exc

    labels: list[str] = []
    for item in (form or {}).get("body", []):
        if item.get("type") == "markdown":
            continue
        if item.get("validations", {}).get("required") is True:
            label = item.get("attributes", {}).get("label", "").removesuffix("*").strip()
            if label:
                labels.append(label)

    # #1346 AC9: authoring 経路 (create_issue_txn.py 経由の implementation body 生成) では
    # ISSUE_TEMPLATE の validations.required フラグに関わらず Required Design References を
    # 必須化する。validate_issue_body.py の _load_required_section_labels() と同じ特例を
    # ここでも適用し、必須セクション一覧が乖離しないようにする（implementation.yml の
    # required-design-references は AC7 の理由で validations.required: false のため）。
    if kind == "implementation" and "Required Design References" not in labels:
        labels.append("Required Design References")

    _require(len(labels) > 0, f"ISSUE_TEMPLATE/{kind}.yml yields no required labels")
    return labels


def _render_machine_readable_contract(child: dict, parent_issue: int) -> str:
    # Medium 1: build the MRC via yaml.safe_dump so embedded quotes/colons/newlines in
    # goal_ref cannot break the YAML or inject extra keys.
    kind = child["kind"]
    sections = child.get("sections", {})
    mrc = {
        "contract_schema_version": "v1",
        "issue_kind": kind,
        "parent_issue": f"#{parent_issue}",
        "goal_ref": sections.get("goal_ref", "delivery-rollup child goal"),
        "change_kind": sections.get("change_kind", "code"),
    }
    dumped = yaml.safe_dump(mrc, sort_keys=False, allow_unicode=True).rstrip("\n")
    return "```yaml\n" + dumped + "\n```"


def _render_acceptance_criteria(child: dict) -> str:
    lines = []
    for ac in child["acceptance_criteria"]:
        text = child.get("sections", {}).get("ac_text", {}).get(ac, "")
        suffix = f": {text}" if text else ""
        lines.append(f"- [ ] {ac}{suffix}")
    return "\n".join(lines)


def _render_verification_commands(child: dict) -> str:
    lines = []
    for ac in child["acceptance_criteria"]:
        cmd = child["verification_commands"][ac]
        lines.append(f"# {ac}\n{cmd}")
    return "\n".join(lines)


def _render_allowed_paths(child: dict) -> str:
    return "\n".join(f"- `{p}`" for p in child["allowed_paths"])


def render_canonical_body(child: dict, parent_issue: int) -> str:
    """Render a template-compliant issue body from the child plan entry (AC2)."""
    kind = child["kind"]
    labels = required_section_labels(kind)
    sections = child.get("sections", {})
    body_parts: list[str] = []

    for label in labels:
        if label == "Machine-Readable Contract":
            content = _render_machine_readable_contract(child, parent_issue)
        elif label == "Parent Issue":
            content = f"#{parent_issue}"
        elif label == "Acceptance Criteria":
            content = _render_acceptance_criteria(child)
        elif label == "Verification Commands":
            content = "```bash\n" + _render_verification_commands(child) + "\n```"
        elif label == "Allowed Paths":
            content = _render_allowed_paths(child)
        elif label == "Stop Conditions":
            content = (
                "- Allowed Paths 外の変更が必要と判明した場合\n"
                "- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合\n"
                "- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）\n"
                "- 後続 Phase / 別スコープへの波及が判明した場合\n"
                "- nested SubAgent delegation が必要になった場合\n"
                "- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合"
            )
        elif label == "Outcome":
            content = sections.get("Outcome") or sections.get("outcome") or child["title"]
        elif label in sections:
            content = sections[label]
        elif label in _DEFAULT_SECTION_TEXT:
            content = _DEFAULT_SECTION_TEXT[label]
        else:
            content = sections.get(label, "なし")
        body_parts.append(f"## {label}\n\n{content}")

    return "\n\n".join(body_parts) + "\n"


# --------------------------------------------------------------------------------------
# Runners (dependency-injected for testability; AC6 fake-gh integration)
# --------------------------------------------------------------------------------------

@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str = ""


ValidateRunner = Callable[[str, str, str], RunResult]
CreateRunner = Callable[..., RunResult]
GhRunner = Callable[[list[str]], RunResult]


def _default_validate_runner(body: str, kind: str, title: str) -> RunResult:
    script = _SCRIPT_DIR / "validate_issue_body.py"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name
    try:
        cp = subprocess.run(
            [sys.executable, str(script), "--body-file", body_file, "--kind", kind, "--title", title],
            capture_output=True,
            text=True,
        )
        return RunResult(cp.returncode, cp.stdout, cp.stderr)
    finally:
        Path(body_file).unlink(missing_ok=True)


def _default_create_runner(
    *,
    repo: str,
    title: str,
    body: str,
    kind: str,
    label_profile: str,
    dependencies: list[int],
    parent_issue: int,
    gh_bin: str,
) -> RunResult:
    """Create an issue through create_issue_txn.py — the ONLY creation path (AC6)."""
    script = _SCRIPT_DIR / "create_issue_txn.py"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name
    try:
        args = [
            sys.executable,
            str(script),
            "--repo",
            repo,
            "--title",
            title,
            "--body-file",
            body_file,
            "--issue-kind",
            kind,
            "--label-profile",
            label_profile,
            "--gh",
            gh_bin,
        ]
        if parent_issue:
            args += ["--parent-issue", str(parent_issue)]
        for dep in dependencies:
            args += ["--dependency", str(dep)]
        cp = subprocess.run(args, capture_output=True, text=True)
        return RunResult(cp.returncode, cp.stdout, cp.stderr)
    finally:
        Path(body_file).unlink(missing_ok=True)


def _default_gh_runner(args: list[str], gh_bin: str = "gh") -> RunResult:
    cp = subprocess.run([gh_bin, *args], capture_output=True, text=True)
    return RunResult(cp.returncode, cp.stdout, cp.stderr)


@dataclass
class Runners:
    validate: ValidateRunner = _default_validate_runner
    create: CreateRunner = _default_create_runner
    gh: GhRunner = _default_gh_runner


def _default_runners(gh_bin: str) -> Runners:
    """Build runners whose default gh path is bound to ``gh_bin`` (Blocker 3: CLI --gh)."""
    return Runners(
        validate=_default_validate_runner,
        create=_default_create_runner,
        gh=lambda a: _default_gh_runner(a, gh_bin),
    )


# --------------------------------------------------------------------------------------
# Parent checklist patch (AC5)
# --------------------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_CHILD_ISSUES_HEADING_RE = re.compile(r"^##\s+Child Issues\s*$", re.IGNORECASE)
_ANY_H2_RE = re.compile(r"^##\s+\S")


def _extract_child_issues_section(body: str) -> tuple[int, int]:
    """Return (start_line, end_line) of the ``## Child Issues`` section body (0-based, end
    exclusive). High 3: the heading is matched with an exact regex (so ``## Child Issues
    Archive`` does not match) and the section must appear exactly once."""
    lines = body.split("\n")
    starts = [i for i, ln in enumerate(lines) if _CHILD_ISSUES_HEADING_RE.match(ln)]
    _require(len(starts) == 1, f"parent body must contain exactly one '## Child Issues' section (found {len(starts)})")
    start = starts[0] + 1
    end = len(lines)
    for j in range(start, len(lines)):
        if _ANY_H2_RE.match(lines[j]):
            end = j
            break
    return start, end


@dataclass
class ParentPatchResult:
    updated: bool
    error: Optional[str] = None


def apply_parent_checklist_patch(
    *,
    repo: str,
    parent_issue: int,
    updates: list[dict],
    expected_body_sha256: Optional[str],
    runners: Runners,
) -> ParentPatchResult:
    """Apply line-oriented patches to the parent ``## Child Issues`` section (AC5)."""
    if not updates:
        return ParentPatchResult(updated=False)

    # Write-zero for a wrong repo even when called directly (before any runner call).
    try:
        _require_trusted_repo(repo)
    except PlanValidationError as exc:
        return ParentPatchResult(updated=False, error=str(exc))

    # body_sha256 is required at this point (validate_plan enforces it for non-empty
    # updates); guard defensively so a direct caller cannot skip the precondition.
    if not (isinstance(expected_body_sha256, str) and _SHA256_RE.match(expected_body_sha256)):
        return ParentPatchResult(updated=False, error="parent.body_sha256 is required to patch the parent")

    view = runners.gh(["issue", "view", str(parent_issue), "--repo", repo, "--json", "body", "--jq", ".body"])
    if view.returncode != 0:
        return ParentPatchResult(updated=False, error=f"parent view failed: {view.stderr.strip()}")
    body = view.stdout
    body = body[:-1] if body.endswith("\n") else body

    for upd in updates:
        for key in ("old_line", "new_line"):
            line = upd.get(key)
            if not isinstance(line, str) or _CTRL_RE.search(line) or re.search(r"[\r\n\x85\u2028\u2029]", line):
                return ParentPatchResult(updated=False, error=f"{key} must be a single line without control characters")

    normalized = expected_body_sha256.split("sha256:")[-1]
    actual = _sha256(body)
    if actual != normalized:
        return ParentPatchResult(
            updated=False,
            error=f"parent body_sha256 mismatch: expected {normalized[:12]} got {actual[:12]}",
        )

    if re.search(r"[\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]", body):
        # splitlines()/join would silently rewrite these line breaks in untouched sections.
        return ParentPatchResult(updated=False, error="parent body has non-LF line breaks; refusing to patch")
    try:
        start, end = _extract_child_issues_section(body)
    except PlanValidationError as exc:
        return ParentPatchResult(updated=False, error=str(exc))
    lines = body.split("\n")
    new_lines = list(lines)
    for upd in updates:
        old_line = upd["old_line"]
        new_line = upd["new_line"]
        match_idxs = [i for i in range(start, end) if new_lines[i] == old_line]
        if len(match_idxs) != 1:
            return ParentPatchResult(
                updated=False,
                error=f"expected_match_count != 1 for old_line {old_line!r} (found {len(match_idxs)})",
            )
        new_lines[match_idxs[0]] = new_line

    new_body = "\n".join(new_lines)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(new_body)
        body_file = f.name
    try:
        edit = runners.gh(["issue", "edit", str(parent_issue), "--repo", repo, "--body-file", body_file])
        if edit.returncode != 0:
            return ParentPatchResult(updated=False, error=f"parent edit failed: {edit.stderr.strip()}")
    finally:
        Path(body_file).unlink(missing_ok=True)

    # Post-edit read-back, section-scoped.
    rb = runners.gh(["issue", "view", str(parent_issue), "--repo", repo, "--json", "body", "--jq", ".body"])
    if rb.returncode != 0:
        return ParentPatchResult(updated=False, error="parent read-back view failed")
    rb_body = rb.stdout
    rb_body = rb_body[:-1] if rb_body.endswith("\n") else rb_body
    try:
        rb_start, rb_end = _extract_child_issues_section(rb_body)
    except PlanValidationError:
        return ParentPatchResult(updated=False, error="parent read-back: Child Issues section missing")
    if rb_body != new_body:
        return ParentPatchResult(updated=False, error="parent read-back body differs from the patched body")
    rb_section = "\n".join(rb_body.split("\n")[rb_start:rb_end])
    for upd in updates:
        if upd["new_line"] not in rb_section or upd["old_line"] in rb_section:
            return ParentPatchResult(updated=False, error="parent read-back did not confirm patch")
    return ParentPatchResult(updated=True)


# --------------------------------------------------------------------------------------
# Materialize (orchestration, AC2-AC8) → CHILD_MATERIALIZATION_RESULT_V2 (AC7)
# --------------------------------------------------------------------------------------

def _decide_status(created: list, errors: list, escalations: list) -> str:
    """Blocker 1: a mixed outcome (some created, but errors OR escalations present) is NOT
    ``ok`` — it is ``partial_failure`` so the parent checklist is never marked complete
    while work remains."""
    if created and (errors or escalations):
        return "partial_failure"
    if errors:
        return "failed"
    if escalations:
        return "human_escalation"
    return "ok"


def _parse_created_issue(stdout: str) -> Optional[dict]:
    """Extract issue number/url/status from create_issue_txn.py JSON stdout (last JSON line)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        num = obj.get("issue_number") or obj.get("number")
        url = obj.get("issue_url") or obj.get("url")
        if num:
            try:
                number = int(num)
            except (TypeError, ValueError):
                return None
            return {"issue_number": number, "issue_url": url, "status": obj.get("status")}
    return None


def materialize(plan: dict[str, Any], runners: Optional[Runners] = None, gh_bin: str = "gh") -> dict[str, Any]:
    """Execute the materialization plan and return a CHILD_MATERIALIZATION_RESULT_V2 dict."""
    plan = validate_plan(plan)
    # Write-zero for a wrong / missing / malformed repo: rejected before any runner call.
    _require_trusted_repo(plan["repo"])
    runners = runners or _default_runners(gh_bin)

    repo = plan["repo"]
    parent_issue = plan["parent"]["issue_number"]
    parent_sha = plan["parent"].get("body_sha256")

    created_issues: list[dict] = []
    affected_issues: list[dict] = []
    errors: list[dict] = []
    escalation_items: list[dict] = []

    # Overlap gate (AC8) — runs before any mutation.
    gate = evaluate_overlap_gate(plan)
    if not gate.ok:
        return {
            "schema": "CHILD_MATERIALIZATION_RESULT_V2",
            "status": "human_escalation",
            "created_issues": [],
            "affected_issues": [],
            "updated_parent": False,
            "escalation_items": gate.escalations,
            "errors": [],
        }

    for child in plan["children"]:
        action = child["action"]
        child_id = child["child_id"]

        if action == "no_op":
            continue
        if action in ("human_escalation", "register_subissue_or_human_escalation", "reuse_and_update_parent"):
            escalation_items.append(
                {"child_id": child_id, "reason": f"action {action} requires human / edit-issue handling"}
            )
            continue

        # action == create_issue
        try:
            body = render_canonical_body(child, parent_issue)
        except Exception as exc:
            errors.append({"child_id": child_id, "error": f"render failed: {exc}"})
            continue

        kind = child["kind"]
        title = child["title"]

        v = runners.validate(body, kind, title)
        if v.returncode != 0:
            errors.append(
                {"child_id": child_id, "error": f"validate_issue_body failed (exit {v.returncode}): {v.stdout[:300]}"}
            )
            continue

        c = runners.create(
            repo=repo,
            title=title,
            body=body,
            kind=kind,
            label_profile=child.get("label_profile", "standard"),
            dependencies=list(child.get("depends_on", [])),
            parent_issue=parent_issue,
            gh_bin=gh_bin,
        )
        parsed = _parse_created_issue(c.stdout)
        if c.returncode != 0:
            # Medium 4: create_issue_txn may return partial_failure/dedupe with a real
            # issue_number (the issue exists on GitHub). Record it so reconciliation is
            # possible, but treat it as an error so the run is never "ok" / parent-patched.
            if parsed and parsed.get("status") in ("partial_failure", "dedupe"):
                affected_issues.append(
                    {
                        "child_id": child_id,
                        "issue_number": parsed["issue_number"],
                        "issue_url": parsed["issue_url"],
                        "txn_status": parsed["status"],
                    }
                )
                errors.append(
                    {
                        "child_id": child_id,
                        "error": f"create_issue_txn {parsed['status']} but issue #{parsed['issue_number']} exists"
                    }
                )
            else:
                errors.append(
                    {
                        "child_id": child_id,
                        "error": f"create_issue_txn failed (exit {c.returncode}): {c.stderr[:300] or c.stdout[:300]}"
                    }
                )
            continue
        if not parsed:
            errors.append({"child_id": child_id, "error": "could not parse created issue from txn output"})
            continue
        if parsed.get("status") != "success":
            # rc 0 with status dedupe (an existing issue matched the title) or any other
            # non-success status is NOT a creation: record the issue, never report it created.
            affected_issues.append(
                {
                    "child_id": child_id,
                    "issue_number": parsed["issue_number"],
                    "issue_url": parsed["issue_url"],
                    "txn_status": parsed.get("status"),
                }
            )
            errors.append(
                {
                    "child_id": child_id,
                    "error": f"create_issue_txn status {parsed.get('status')!r} is not a creation "
                    f"(issue #{parsed['issue_number']} not reported as created)",
                }
            )
            continue
        created_issues.append(
            {
                "child_id": child_id,
                "issue_number": parsed["issue_number"],
                "issue_url": parsed["issue_url"],
                "action_taken": "create_issue",
            }
        )

    status = _decide_status(created_issues, errors, escalation_items)

    # Parent checklist patch ONLY on a fully clean result (AC7 / Blocker 1): no errors and
    # no escalation items. A mixed run never marks the parent checklist complete.
    updated_parent = False
    if status == "ok" and not errors and not escalation_items:
        patch = apply_parent_checklist_patch(
            repo=repo,
            parent_issue=parent_issue,
            updates=plan.get("parent_body_updates", []),
            expected_body_sha256=parent_sha,
            runners=runners,
        )
        if patch.error:
            errors.append({"child_id": "(parent)", "error": patch.error})
            status = "partial_failure"
        else:
            updated_parent = patch.updated

    return {
        "schema": "CHILD_MATERIALIZATION_RESULT_V2",
        "status": status,
        "created_issues": created_issues,
        "affected_issues": affected_issues,
        "updated_parent": updated_parent,
        "escalation_items": escalation_items,
        "errors": errors,
    }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def _load_plan_file(path: str) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"plan file is not valid JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize delivery-rollup child issues")
    parser.add_argument("--plan-file", required=True, help="Path to CHILD_MATERIALIZATION_PLAN_V2 JSON")
    parser.add_argument("--gh", dest="gh_bin", default="gh", help="gh binary path (for fake-gh integration)")
    args = parser.parse_args(argv)

    try:
        raw = _load_plan_file(args.plan_file)
        plan = validate_plan(raw)
    except PlanValidationError as exc:
        sys.stderr.write(f"PLAN_VALIDATION_ERROR: {exc}\n")
        return 2

    try:
        result = materialize(plan, gh_bin=args.gh_bin)
    except PlanValidationError as exc:
        sys.stderr.write(f"PLAN_VALIDATION_ERROR: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    if result["status"] == "ok":
        return 0
    if result["status"] == "human_escalation":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
