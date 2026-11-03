#!/usr/bin/env python3
"""
Allowed Paths Review Gate (ALLOWED_PATHS_GATE_RESULT_V1)

Deterministically recalculates PR's actual changed files (from git diff)
against linked issue's Allowed Paths snapshot. Producer role: review_subagent.

Key principles:
- Worker transcript / report is NOT an input
- changed_files_source is git_diff_current_merge_base_head (triple-dot merge-base..head)
  before rename-aware canonicalization; after validation it becomes the rename-aware
  source authority (git_diff_name_status_find_renames_z local fallback, or
  github_pull_request_files_api_with_previous_filename when PR files API data is supplied)
- head_sha != reviewed_head_sha -> indeterminate (merge-blocking)
- contract fingerprint and execution context are separated for freshness detection
- review mode requires explicit snapshot bindings and expected fingerprint
- Allowed Paths matcher uses repo-relative POSIX normalization with fail-closed invalid input handling
- rename / previous_filename path provenance (Issue #1300): the post-image
  filename-only list is NOT the canonical input for Allowed Paths determination.
  Canonical input is `audited_paths[]`, derived from structured
  `changed_file_records[]` (ChangedFileRecord), which include both the current
  path and — for renames — the previous path. `gh_pr_diff_name_only` and
  `git_diff_current_merge_base_head_name_only` are insufficient_for_rename_provenance
  and must not be used as the rename provenance source.
- the deterministic local fallback (`git diff --name-status -M -z`, Issue
  #1300 review Blocker 1) IS the canonical source for changed_file_records
  when no PR files API data is supplied -- it is not a best-effort
  enrichment layered on top of `git diff --name-only`. Subprocess failure
  or a parse error (malformed/unknown status) is fail-closed: it raises
  and is converted to `indeterminate` by evaluate(), never silently
  degraded to "no renames" (Issue #1300 review Blocker 2).
- `build_audited_paths()` rejects any ChangedFileRecord with
  `provenance_complete: false` or with a `source` outside
  `{git_diff_name_status_find_renames_z, github_pull_request_files_api_with_previous_filename}`
  as `indeterminate`, even when the path itself is inside Allowed Paths
  (Issue #1300 review Blocker 3 -- otherwise an insufficient-provenance
  record could silently bypass the gate as `ok`).
- NOTE (Issue #1300 review Blocker 4, scope decision): this script does
  NOT implement a GitHub PR files API pagination adapter. `--pr-files-json`
  only accepts an already-paginated JSON object supplied by the caller;
  building that JSON via `gh api --paginate` (or equivalent) is explicitly
  OUT OF SCOPE for this PR and tracked as a follow-up. Today the
  operative, exercised path is the deterministic local fallback
  (`git_diff_name_status_find_renames_z`).
- Issue #1611 AC11: the matcher (`AllowedPathsMatcher`), the repo-relative
  path normalizer, `ChangedFileRecord`, and the `git diff --name-status -M
  -z` parser (`parse_git_diff_name_status_z`) are extracted to
  `scripts/agent-guards/changed_file_matcher.py` and imported here, so this
  review-time gate and the staging-time `controlled_git_change_exec.py`
  executor evaluate Allowed Paths against the exact same grammar -- never
  independently reimplemented copies that could drift apart.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Issue #1611 AC11: shared matcher/normalizer/ChangedFileRecord module lives
# under scripts/agent-guards/ (repo root / scripts / agent-guards), four
# directories up from this file (scripts -> pr-review-judge -> skills ->
# .claude -> repo root).
_AGENT_GUARDS_DIR = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from changed_file_matcher import (  # noqa: E402
    PATH_ROLE_FILENAME,
    PATH_ROLE_PREVIOUS_FILENAME,
    SOURCE_GIT_NAME_STATUS_Z,
    SOURCE_PR_FILES_API,
    AllowedPathsMatcher,
    ChangedFileRecord,
    parse_git_diff_name_status_z,
)


class GateStatus(Enum):
    """Status values for allowed paths gate."""

    OK = "ok"
    FAIL_CLOSED = "fail_closed"
    STALE_SNAPSHOT = "stale_snapshot"
    INDETERMINATE = "indeterminate"


# Issue #1611 AC11: rename provenance source constants, ChangedFileRecord,
# AllowedPathsMatcher, and parse_git_diff_name_status_z now live in
# scripts/agent-guards/changed_file_matcher.py (imported above) so this
# gate and the staging-time controlled executor share one grammar.

_PR_FILES_STATUS_MAP = {
    "added": "added",
    "removed": "removed",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "copied",
    "changed": "type_changed",
    "unchanged": "unchanged",
}


@dataclass
class ExecutionContext:
    """Execution context (audit log only, NOT used for freshness judgment)."""

    worktree_root: str
    generated_at: str
    tool_version: str = "1.5.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContractFingerprint:
    """Contract fingerprint for freshness judgment."""

    issue_number: int
    contract_source_kind: str
    contract_source_id: str
    contract_body_sha256: str
    allowed_paths_normalized_sha256: str
    base_ref: str
    base_sha_at_snapshot: str

    def to_normalized_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass
class AllowedPathsGateResult:
    """ALLOWED_PATHS_GATE_RESULT_V1 schema."""

    produced_at: str
    status: str = ""
    produced_by: str = "allowed_paths_review_gate.py"
    producer_role: str = "review_subagent"
    worker_report_used_as_canonical: bool = False
    pr_number: int = 0
    base_ref: str = ""
    diff_base_sha: str = ""
    base_sha: str = ""
    head_sha: str = ""
    reviewed_head_sha: str = ""
    changed_files_source: str = "git_diff_unvalidated_diff_base_head"
    allowed_paths_source: str = ""
    changed_files_count: int = 0
    changed_files: List[str] = field(default_factory=list)
    changed_file_records: List[Dict[str, Any]] = field(default_factory=list)
    audited_paths: List[Dict[str, Any]] = field(default_factory=list)
    allowed_paths_list: List[str] = field(default_factory=list)
    violations: List[Dict[str, Any]] = field(default_factory=list)
    contract_fingerprint: Dict[str, Any] = field(default_factory=dict)
    execution_context: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "produced_at": self.produced_at,
            "produced_by": self.produced_by,
            "producer_role": self.producer_role,
            "worker_report_used_as_canonical": self.worker_report_used_as_canonical,
            "pr_number": self.pr_number,
            "base_ref": self.base_ref,
            "diff_base_sha": self.diff_base_sha,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "reviewed_head_sha": self.reviewed_head_sha,
            "changed_files_source": self.changed_files_source,
            "allowed_paths_source": self.allowed_paths_source,
            "changed_files_count": self.changed_files_count,
            "changed_files": self.changed_files,
            "changed_file_records": self.changed_file_records,
            "audited_paths": self.audited_paths,
            "allowed_paths_list": self.allowed_paths_list,
            "violations": self.violations,
            "contract_fingerprint": self.contract_fingerprint,
            "execution_context": self.execution_context,
            **({} if self.reason is None else {"reason": self.reason}),
            **({} if not self.errors else {"errors": self.errors}),
            **({} if not self.warnings else {"warnings": self.warnings}),
        }


class AllowedPathsGateEvaluator:
    """Main evaluator for PR allowed paths gate."""

    def __init__(
        self,
        *,
        pr_number: int,
        base_ref: str,
        base_sha_at_snapshot: str,
        current_base_sha: str,
        diff_base_sha: Optional[str] = None,
        head_sha: str,
        reviewed_head_sha: str,
        allowed_paths: List[str],
        contract_body_sha256: str,
        contract_source_kind: str,
        contract_source_id: str,
        expected_contract_fingerprint: Optional[Dict[str, Any]],
        issue_number: int = 0,
        pr_files_data: Optional[Dict[str, Any]] = None,
    ):
        self.pr_number = pr_number
        self.base_ref = base_ref
        self.base_sha_at_snapshot = base_sha_at_snapshot
        self.current_base_sha = current_base_sha
        self.diff_base_sha = diff_base_sha
        self.head_sha = head_sha
        self.reviewed_head_sha = reviewed_head_sha
        self.allowed_paths = allowed_paths
        self.contract_body_sha256 = contract_body_sha256
        self.contract_source_kind = contract_source_kind
        self.contract_source_id = contract_source_id
        self.expected_contract_fingerprint = expected_contract_fingerprint
        self.issue_number = issue_number
        self.pr_files_data = pr_files_data
        self.validated_diff_base_sha: Optional[str] = None

    def canonicalize_allowed_paths(self) -> List[str]:
        canonicalized: List[str] = []
        for pattern in self.allowed_paths:
            normalized = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
            if normalized is None:
                raise ValueError(f"invalid allowed path pattern: {pattern}")
            canonicalized.append(normalized)
        return sorted(set(canonicalized))

    def compute_allowed_paths_hash(self) -> str:
        normalized_json = json.dumps(
            self.canonicalize_allowed_paths(),
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(normalized_json.encode()).hexdigest()

    def compute_contract_fingerprint(self) -> Dict[str, Any]:
        fingerprint = ContractFingerprint(
            issue_number=self.issue_number,
            contract_source_kind=self.contract_source_kind,
            contract_source_id=self.contract_source_id,
            contract_body_sha256=self.contract_body_sha256,
            allowed_paths_normalized_sha256=self.compute_allowed_paths_hash(),
            base_ref=self.base_ref,
            base_sha_at_snapshot=self.base_sha_at_snapshot,
        )
        return json.loads(fingerprint.to_normalized_json())

    def compute_current_merge_base_sha(self) -> str:
        try:
            result = subprocess.run(
                ["git", "merge-base", self.current_base_sha, self.head_sha],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git merge-base failed: {exc.stderr}") from exc
        merge_base_sha = result.stdout.strip()
        if not merge_base_sha:
            raise RuntimeError("git merge-base returned an empty SHA")
        return merge_base_sha

    def validate_diff_base_sha(self) -> str:
        computed_diff_base_sha = self.compute_current_merge_base_sha()
        if self.diff_base_sha and self.diff_base_sha != computed_diff_base_sha:
            raise ValueError(
                "diff_base_sha does not match current merge-base: "
                f"provided={self.diff_base_sha} computed={computed_diff_base_sha}"
            )
        self.validated_diff_base_sha = computed_diff_base_sha
        return computed_diff_base_sha

    def get_changed_files_from_git(self) -> List[str]:
        """DEPRECATED post-image-only alias (git diff --name-only).

        Retained ONLY for external/backward compatibility (e.g. callers
        that need a bare filename list). NOT used internally by
        get_changed_file_records_from_git() / evaluate() -- this source is
        `insufficient_for_rename_provenance` (Issue #1300) because it
        cannot represent rename previous_filename. Canonical determination
        uses get_changed_file_records() / ChangedFileRecord, whose local
        fallback is built directly from `git diff --name-status -M -z`
        (see get_changed_file_records_from_git()), never from this method.
        """
        if not self.validated_diff_base_sha:
            raise RuntimeError("validated diff base SHA is unavailable")
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{self.validated_diff_base_sha}...{self.head_sha}"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git diff failed: {exc.stderr}") from exc
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def get_changed_file_records_from_git(self) -> List[ChangedFileRecord]:
        """Deterministic local fallback -- CANONICAL source when no PR files
        API data is supplied (Issue #1300 Blocker 1 remediation).

        Built directly from `git diff --name-status -M -z <base>...<head>`,
        NOT from the post-image-only `get_changed_files_from_git()` alias.
        `-z` avoids newline/tab ambiguity in paths (security-sensitive
        gate). `-M` enables rename detection so R* records carry both old
        and new paths.

        Fail-closed (Issue #1302 review Blocker 2): a subprocess failure or
        a parse error (malformed/unknown status token) is NEVER silently
        degraded into an empty rename map / `modified` status. Both
        failure modes raise and propagate to the caller, which
        (`evaluate()`) converts any exception here into `indeterminate` --
        it must never be treated as "no renames occurred".
        """
        if not self.validated_diff_base_sha:
            raise RuntimeError("validated diff base SHA is unavailable")
        try:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-status",
                    "-M",
                    "-z",
                    f"{self.validated_diff_base_sha}...{self.head_sha}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "git diff --name-status -M -z failed (fail-closed -- NOT "
                f"treated as no-renames): {exc.stderr}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"git diff --name-status -M -z could not be executed (fail-closed): {exc}"
            ) from exc
        # parse_git_diff_name_status_z raises ValueError on malformed/unknown
        # status tokens -- intentionally NOT caught here (fail-closed).
        return parse_git_diff_name_status_z(result.stdout, source=SOURCE_GIT_NAME_STATUS_Z)

    def get_changed_file_records_from_pr_files_api(self) -> List[ChangedFileRecord]:
        """Preferred oracle: GitHub PR files API records (external input).

        Requires the caller to have completed pagination and to supply
        `previous_filename` for every `status: renamed` record; otherwise
        this raises (evaluate() converts this into `indeterminate`).
        """
        data = self.pr_files_data
        if not isinstance(data, dict):
            raise ValueError("pr_files_json must be a JSON object")
        if not data.get("pagination_complete", False):
            raise ValueError(
                f"{SOURCE_PR_FILES_API} pagination incomplete: pagination_complete must be true"
            )
        if data.get("file_limit_reached", False):
            raise ValueError(f"{SOURCE_PR_FILES_API} file_limit_reached is true")
        raw_records = data.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("pr_files_json 'records' must be a list")

        records: List[ChangedFileRecord] = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                raise ValueError("malformed pr_files_json record: not an object")
            filename = raw.get("filename")
            status_raw = raw.get("status")
            previous_filename = raw.get("previous_filename")
            if not filename or not status_raw:
                raise ValueError("pr_files_json record missing filename/status")
            if status_raw not in _PR_FILES_STATUS_MAP:
                raise ValueError(f"unknown pr_files_json status: {status_raw!r}")
            status = _PR_FILES_STATUS_MAP[status_raw]
            if status == "renamed" and not previous_filename:
                raise ValueError(
                    f"renamed record missing previous_filename for {filename!r} "
                    f"({SOURCE_PR_FILES_API})"
                )
            records.append(
                ChangedFileRecord(
                    path=filename,
                    status=status,
                    previous_path=previous_filename,
                    source=SOURCE_PR_FILES_API,
                    provenance_complete=True,
                )
            )
        return records

    def get_changed_file_records(self) -> List[ChangedFileRecord]:
        """Canonical changed-file record source dispatch.

        Uses the preferred oracle (GitHub PR files API, when
        --pr-files-json was supplied); otherwise falls back to the
        deterministic local `git diff --name-status -M -z` parser.
        """
        if self.pr_files_data is not None:
            return self.get_changed_file_records_from_pr_files_api()
        return self.get_changed_file_records_from_git()

    @staticmethod
    def build_audited_paths(
        records: List[ChangedFileRecord],
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Build audited_paths[] from changed_file_records[].

        Returns (audited_paths, indeterminate_reason). If
        indeterminate_reason is not None, the caller must treat the whole
        gate result as `indeterminate` (a rename record with unavailable
        previous_path, or an invalid repo-relative path, must never be
        silently dropped or treated as `ok`).
        """
        audited: List[Dict[str, Any]] = []
        for idx, record in enumerate(records):
            if record.status == "renamed" and not record.previous_path:
                return [], (
                    f"rename record missing previous_path for {record.path!r} "
                    "(indeterminate — filename-only fallback is forbidden for renames)"
                )
            if not record.provenance_complete:
                return [], (
                    f"changed file provenance incomplete for {record.path!r} "
                    "(indeterminate — insufficient/incomplete source must not be "
                    "silently treated as ok)"
                )
            if record.source not in (SOURCE_GIT_NAME_STATUS_Z, SOURCE_PR_FILES_API):
                return [], (
                    f"insufficient or unknown changed file source {record.source!r} "
                    f"for {record.path!r} (indeterminate — name-only/unrecognized "
                    "sources must not be treated as ok)"
                )
            normalized_path = AllowedPathsMatcher.normalize_path(record.path)
            if normalized_path is None:
                return [], f"invalid path in changed file record: {record.path!r} (indeterminate)"
            audited.append(
                {
                    "path": normalized_path,
                    "path_role": PATH_ROLE_FILENAME,
                    "source_record_index": idx,
                }
            )
            if record.previous_path:
                normalized_previous = AllowedPathsMatcher.normalize_path(record.previous_path)
                if normalized_previous is None:
                    return [], (
                        f"invalid previous_path in changed file record: "
                        f"{record.previous_path!r} (indeterminate)"
                    )
                audited.append(
                    {
                        "path": normalized_previous,
                        "path_role": PATH_ROLE_PREVIOUS_FILENAME,
                        "source_record_index": idx,
                    }
                )
        return audited, None

    def evaluate(self) -> AllowedPathsGateResult:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        result = AllowedPathsGateResult(
            produced_at=now,
            pr_number=self.pr_number,
            base_ref=self.base_ref,
            head_sha=self.head_sha,
            reviewed_head_sha=self.reviewed_head_sha,
        )

        if self.head_sha != self.reviewed_head_sha:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = (
                f"head_sha mismatch: {self.head_sha} != {self.reviewed_head_sha} (merge-blocking)"
            )
            result.errors.append(result.reason)
            return result

        if not self.allowed_paths:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = "Allowed Paths snapshot is missing or empty (merge-blocking)"
            result.errors.append(result.reason)
            return result

        # contract_source_kind/source_id and expected_contract_fingerprint are
        # advisory telemetry (Issue #1873 / #1860 Owner Decision): the Allowed
        # Paths pattern source of truth is the live linked issue body, not the
        # contract snapshot/fingerprint binding. Their absence is recorded as
        # a warning and does NOT block the gate -- only an actual Allowed
        # Paths violation (fail_closed) or unresolved rename provenance
        # (indeterminate) may block.
        if not self.contract_source_kind or not self.contract_source_id:
            result.warnings.append("contract_source_kind/source_id_missing (advisory)")

        if self.expected_contract_fingerprint is None:
            result.warnings.append("expected_contract_fingerprint_missing (advisory)")

        try:
            result.allowed_paths_list = self.canonicalize_allowed_paths()
            if self.expected_contract_fingerprint is not None:
                result.contract_fingerprint = self.compute_contract_fingerprint()
        except Exception as exc:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to compute contract fingerprint: {exc}"
            result.errors.append(result.reason)
            return result

        if (
            self.expected_contract_fingerprint is not None
            and self.expected_contract_fingerprint != result.contract_fingerprint
        ):
            # Advisory only: fingerprint drift does not block. Re-evaluate
            # against the live allowed_paths (already canonicalized above)
            # and record the drift for audit purposes.
            result.warnings.append(
                "stale_snapshot: contract fingerprint diverged from live allowed_paths (advisory, non-blocking)"
            )

        try:
            validated_diff_base_sha = self.validate_diff_base_sha()
        except Exception as exc:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to validate diff base: {exc}"
            result.errors.append(result.reason)
            return result

        result.diff_base_sha = validated_diff_base_sha
        result.base_sha = validated_diff_base_sha
        result.changed_files_source = (
            SOURCE_PR_FILES_API if self.pr_files_data is not None else SOURCE_GIT_NAME_STATUS_Z
        )

        try:
            changed_file_records = self.get_changed_file_records()
        except Exception as exc:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to get changed file records: {exc}"
            result.errors.append(result.reason)
            return result

        audited_paths, indeterminate_reason = self.build_audited_paths(changed_file_records)
        if indeterminate_reason is not None:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = indeterminate_reason
            result.errors.append(result.reason)
            return result

        result.changed_file_records = [record.to_dict() for record in changed_file_records]
        result.audited_paths = audited_paths
        # Backward-compatible post-image alias — NOT the canonical input for
        # Allowed Paths determination (see audited_paths / build_audited_paths).
        result.changed_files = list(dict.fromkeys(record.path for record in changed_file_records))
        result.changed_files_count = len(result.changed_files)
        result.allowed_paths_source = "live_linked_issue_body"

        try:
            result.execution_context = ExecutionContext(
                worktree_root=str(Path.cwd()),
                generated_at=now,
            ).to_dict()
        except Exception as exc:
            result.execution_context = {
                "worktree_root": "unknown",
                "generated_at": now,
                "error": str(exc),
            }

        violations = []
        for entry in audited_paths:
            if not AllowedPathsMatcher.is_file_allowed(entry["path"], result.allowed_paths_list):
                violations.append(
                    {
                        "file": entry["path"],
                        "path_role": entry["path_role"],
                        "reason": "not in allowed_paths",
                    }
                )

        result.violations = violations
        if violations:
            result.status = GateStatus.FAIL_CLOSED.value
            result.reason = f"{len(violations)} file(s) outside allowed paths"
        else:
            result.status = GateStatus.OK.value
            result.reason = "All changed files are within allowed paths"
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate PR allowed paths gate deterministically")
    parser.add_argument("--pr-number", type=int, required=True, help="PR number")
    parser.add_argument("--base-ref", required=True, help="Base branch name (e.g., main)")
    parser.add_argument(
        "--base-sha-at-snapshot",
        default="",
        help="Advisory: snapshot freshness SHA from the linked issue contract (optional, non-canonical)",
    )
    parser.add_argument(
        "--current-base-sha",
        required=True,
        help="Current base branch tip SHA used to validate the local fallback merge-base",
    )
    parser.add_argument(
        "--diff-base-sha",
        help="Optional externally supplied merge-base SHA; must equal git merge-base(current_base_sha, head_sha)",
    )
    parser.add_argument("--head-sha", required=True, help="Current head SHA")
    parser.add_argument("--reviewed-head-sha", required=True, help="Head SHA at review time")
    parser.add_argument(
        "--allowed-paths",
        type=json.loads,
        required=True,
        help="JSON array of allowed paths from issue contract",
    )
    parser.add_argument("--contract-body-sha256", default="", help="Advisory: SHA256 of issue contract body (optional)")
    parser.add_argument("--contract-source-kind", default=None, help="Advisory: contract source kind (optional)")
    parser.add_argument("--contract-source-id", default=None, help="Advisory: contract source identifier (optional)")
    parser.add_argument(
        "--expected-contract-fingerprint",
        type=json.loads,
        default=None,
        help="Advisory: JSON object captured at snapshot time (optional; fingerprint drift is non-blocking)"
    )
    parser.add_argument("--issue-number", type=int, default=0, help="Linked issue number")
    parser.add_argument(
        "--pr-files-json",
        type=json.loads,
        default=None,
        help=(
            "Optional JSON object representing the GitHub PR files API response: "
            '{"records": [{"filename": str, "status": str, '
            '"previous_filename": str|null}], "pagination_complete": bool, '
            '"file_limit_reached": bool}. When supplied this is the preferred '
            f"oracle ({SOURCE_PR_FILES_API}); otherwise the deterministic local "
            f"fallback ({SOURCE_GIT_NAME_STATUS_Z}) is used."
        ),
    )
    parser.add_argument("--format", choices=["json", "yaml"], default="json", help="Output format")
    args = parser.parse_args()

    evaluator = AllowedPathsGateEvaluator(
        pr_number=args.pr_number,
        base_ref=args.base_ref,
        base_sha_at_snapshot=args.base_sha_at_snapshot,
        current_base_sha=args.current_base_sha,
        diff_base_sha=args.diff_base_sha,
        head_sha=args.head_sha,
        reviewed_head_sha=args.reviewed_head_sha,
        allowed_paths=args.allowed_paths,
        contract_body_sha256=args.contract_body_sha256,
        contract_source_kind=args.contract_source_kind,
        contract_source_id=args.contract_source_id,
        expected_contract_fingerprint=args.expected_contract_fingerprint,
        issue_number=args.issue_number,
        pr_files_data=args.pr_files_json,
    )

    result = evaluator.evaluate()
    result_dict = result.to_dict()

    if args.format == "yaml":
        try:
            import yaml
            print(yaml.dump(result_dict, default_flow_style=False, sort_keys=False))
        except ImportError:
            print("PyYAML not available, using JSON format instead", file=sys.stderr)
            print(json.dumps(result_dict, indent=2))
    else:
        print(json.dumps(result_dict, indent=2))

    sys.exit(0 if result.status == GateStatus.OK.value else 1)


if __name__ == "__main__":
    main()
