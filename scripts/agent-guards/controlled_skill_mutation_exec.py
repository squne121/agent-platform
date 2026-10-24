"""
controlled_skill_mutation_exec.py

Single executor for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY entries.
Invoked by agents via the exact argv form defined in controlled_skill_mutation_policy.py.

Design: Direct script allow for ensure_contract_snapshot.py is denied. Only this
executor is allow-listed in settings.json. It handles the issue-metadata mutation
lane (Issue #1284): issue_body.update / issue_content.update / issue_comment.publish /
contract_snapshot.publish / test_verdict.publish / issue_scope_snapshot.materialize /
issue_dependency.remove. The executor enforces:
  - command_id whitelist (ALL_COMMAND_IDS)
  - repo binding (--repo must be TRUSTED_REPO)
  - git remote origin binding (must match TRUSTED_REPO)
  - issue binding (--issue-number must match LOOP_ISSUE_NUMBER env when present)
  - input-file binding (must be in the active issue/command-id artifact subtree,
    no symlinks, no hardlinks)
  - input-file JSON validation (schema + issue_number field cross-check, plus
    per-command-id field schemas)
  - gh binary discovery (trusted path only)
  - environment sanitization (PYTHONPATH / PYTHONHOME / GH_EDITOR / EDITOR /
    VISUAL / BROWSER overridden/removed)
  - module realpath inspection (ensure_contract_snapshot.py / run_contract_review_once.py /
    contract_review_result_parser.py canonical path check for contract_snapshot.publish --
    missing=deny)
  - remote-state-is-authority idempotency: local marker files are cache/audit only.
    issue_body.update and issue_comment.publish always readback GitHub before
    declaring success; a local marker never substitutes for a remote check.
  - pre-mutation marker precheck for issue_comment.publish (no POST before remote
    marker state is known -- a failed transaction must not leave a side effect)
  - postcondition (git status --porcelain=v1 must show no changes outside the
    command-id-scoped artifact write root)
  - comment read-back by marker (comment id / url / body hash recorded)

Exit codes:
  0 - publish succeeded
  1 - publish failed, stale/mismatched state detected, or idempotency marker already set
  2 - validation error (wrong args, wrong issue, wrong file, missing schema fields, etc.)

Issue #1166 / Issue #1284.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re as _re
import shutil
import stat as _stat
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from urllib.parse import urlsplit

# -- Path resolution -----------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
# scripts/agent-guards/ -> scripts/ -> project_root
PROJECT_ROOT = _THIS_FILE.parent.parent.parent

# -- Import shared policy ------------------------------------------------------

sys.path.insert(0, str(_THIS_FILE.parent))
from controlled_skill_mutation_policy import (
    ALL_COMMAND_IDS,
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
    COMMAND_ID_ISSUE_BODY_UPDATE,
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    COMMAND_ID_ISSUE_CONTENT_UPDATE,
    COMMAND_ID_ISSUE_DEPENDENCY_REMOVE,
    COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE,
    COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
    COMMAND_ID_TEST_VERDICT_PUBLISH,
    ENV_BINDING_MANDATORY_COMMAND_IDS,
    ENV_SANITIZE_KEYS,
    INPUT_SCHEMA_BY_COMMAND,
    ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS,
    ISSUE_METADATA_NAMESPACE_SEGMENT,
    ISSUE_RELATIONSHIP_UPDATE_MAX_TOTAL_NODES,
    validate_issue_dependency_remove_input,
    validate_issue_relationship_update_input,
)
from repo_identity import load_trusted_repo_slug

# Single source of truth: config/repository_identity.v1.json. An unusable config raises
# RepositoryIdentityError at import, before any gh / git call can exist (fail-closed).
TRUSTED_REPO = load_trusted_repo_slug()

# Stage 2 approved live operations: Issue body update and Issue comment publish. Every other
# command_id stays registered (inert data) but is refused before any git / gh call.
APPROVED_LIVE_COMMAND_IDS = frozenset(
    {
        COMMAND_ID_ISSUE_BODY_UPDATE,
        COMMAND_ID_ISSUE_CONTENT_UPDATE,
        COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    }
)

_ENSURE_CONTRACT_SNAPSHOT_REL = (
    ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
)
_RUN_CONTRACT_REVIEW_ONCE_REL = (
    ".claude/skills/issue-contract-review/scripts/run_contract_review_once.py"
)
_EVALUATE_PRODUCT_SPEC_GATE_REL = (
    ".claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py"
)
_CONTRACT_REVIEW_RESULT_PARSER_REL = (
    ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py"
)
_ISSUE_SCOPE_SNAPSHOT_MATERIALIZER_REL = "scripts/agent-guards/materialize_issue_scope_snapshot.py"

# -- Result schema -------------------------------------------------------------

RESULT_SCHEMA = "CONTROLLED_SKILL_MUTATION_RESULT_V1"

# -- gh binary discovery -------------------------------------------------------

_GH_TRUSTED_PATHS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def _find_gh_bin() -> tuple[str | None, str]:
    """Find gh binary in trusted PATH. Returns (path, error)."""
    gh = shutil.which("gh", path=_GH_TRUSTED_PATHS)
    if not gh:
        return None, "gh_not_found_in_trusted_path"
    return gh, ""


# -- Git remote origin verification --------------------------------------------


# Issue #1539 fix_delta Blocker 2: the only trusted remote host. Structural
# scheme/host validation replaces the previous "grab the last owner/repo-shaped
# path segment" regex, which ignored host/scheme entirely and would treat
# `https://attacker.example/<owner>/<repo>.git` as trusted.
_TRUSTED_GITHUB_HOST = "github.com"
_OWNER_REPO_RE = _re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _normalize_owner_repo(path: str) -> str | None:
    path = path.strip("/")
    path = path.removesuffix(".git")
    if not path or not _OWNER_REPO_RE.match(path):
        return None
    return path


def _parse_trusted_github_remote(url: str) -> str | None:
    """Return the normalized ``owner/repo`` iff url is a canonical HTTPS/SSH
    github.com remote. Returns None for any other host, scheme, port, or
    non-``git``/anonymous userinfo (evil host, file://, other-host SSH, etc.).
    """
    url = (url or "").strip()
    if not url or "\x00" in url:
        return None
    if "://" in url:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        if parsed.scheme.lower() not in ("https", "ssh"):
            return None
        host = (parsed.hostname or "").lower()
        if host != _TRUSTED_GITHUB_HOST:
            return None
        if parsed.port not in (None, 443, 22):
            return None
        if parsed.username not in (None, "git"):
            return None
        return _normalize_owner_repo(parsed.path)
    # scp-like syntax: [user@]host:path (e.g. git@github.com:owner/repo.git)
    m = _re.match(r"^(?:([A-Za-z0-9_.-]+)@)?([A-Za-z0-9_.-]+):(.+)$", url)
    if not m:
        return None
    user, host, path = m.group(1), m.group(2), m.group(3)
    if user not in (None, "git"):
        return None
    if host.lower() != _TRUSTED_GITHUB_HOST:
        return None
    return _normalize_owner_repo(path)


def _verify_git_remote_origin(
    project_root: Path, trusted_repo: str, env: dict[str, str] | None = None
) -> str:
    """Return empty string if origin is a canonical github.com/trusted_repo
    HTTPS or SSH remote, else a descriptive error string."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if out.returncode != 0:
            return f"git_remote_origin_failed: {out.stderr.strip()[:100]}"
        url = out.stdout.strip()
        normalized = _parse_trusted_github_remote(url)
        if normalized is None:
            return f"git_remote_origin_untrusted_host_or_scheme: {url!r}"
        if normalized != trusted_repo:
            return f"git_remote_origin_mismatch: {normalized!r} != {trusted_repo!r}"
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"git_remote_origin_exception: {exc}"


# -- Issue #1284 Blocker 5: generic metadata-command env sanitizer -------------

# Issue #2340 fix_delta P0-1 (PR #2357 review, 2026-08-27): sanitize
# execution/log-hygiene NOISE only, never the GitHub credential carrier
# itself. The original #2340 P0 fix pointed `_build_metadata_sanitized_env()`
# at the generic `ENV_SANITIZE_KEYS` list (which strips GH_TOKEN /
# GITHUB_TOKEN / GH_CONFIG_DIR -- a policy #1667 introduced for the
# higher-trust PR-review / dependency-graph mutation lanes, see
# `_build_pr_review_gh_env()` / `_build_issue_dependency_remove_gh_env()`
# below, which still use `ENV_SANITIZE_KEYS` unchanged). Applying that same
# strip-the-credential policy to the issue-metadata read/write helpers this
# function serves reversed the direction #2299 / PR #2303 established for
# the Claude-GPT launcher: that Issue explicitly changed the launcher to
# share `GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR` from the native
# ambient environment (while `HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME`
# stay isolated), specifically so downstream `gh` invocations authenticate.
# Stripping those three keys here made every controlled write in this
# family credential-starved again -- functionally reintroducing the
# completion-rate regression #2299 fixed, just one layer further downstream.
#
# The correct separation of concerns is "credential availability" vs.
# "output/log hygiene": this function keeps the credential carrier
# (`GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR`) intact and only strips
# execution-noise / redirection-surface variables that have no legitimate
# reason to reach a `gh api --hostname github.com ...` call this module
# already argv-pins to the trusted host. Token VALUES are still never
# permitted to leak into stdout/stderr/artifacts: `_classify_gh_error()` /
# `_redact_secret_like_tokens()` redact token-shaped substrings from
# captured `gh` stderr before it is ever returned or logged.
_METADATA_ENV_NOISE_STRIP_KEYS = (
    "PUBLISH_ARTIFACT_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "GH_EDITOR",
    "EDITOR",
    "VISUAL",
    "BROWSER",
    # GH_HOST / GH_REPO: every call site below already argv-pins
    # `--hostname github.com`, so an ambient override cannot silently
    # redirect these particular calls -- stripped anyway as defense in
    # depth against any future call site that omits the explicit flag.
    "GH_HOST",
    "GH_REPO",
    "GH_DEBUG",
    "DEBUG",
)


def _build_metadata_sanitized_env() -> dict[str, str]:
    """Build the sanitized environment for issue-metadata read/write
    subprocesses (issue_body.update / issue_content.update /
    issue_comment.publish / contract_snapshot.publish /
    issue_scope_snapshot.materialize). Strips execution/log-hygiene noise
    only (Issue #2340 fix_delta P0-1) -- the GitHub credential carrier
    (`GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR`) is deliberately left
    intact so these calls authenticate the same way the Claude-GPT launcher
    itself does (#2299 / PR #2303).
    """
    env = os.environ.copy()
    for key in _METADATA_ENV_NOISE_STRIP_KEYS:
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


# -- Issue #1284 Blocker 5: contract_snapshot.publish module realpath check ----


def _check_contract_snapshot_module_realpaths(project_root: Path) -> list[str]:
    """Return list of realpath violations for the contract_snapshot.publish
    publisher module chain. Missing modules are treated as errors (missing=deny).

    Issue #1459 review Blocker (evaluator_missing_from_module_trust_chain):
    evaluate_product_spec_gate.py is imported by ensure_contract_snapshot.py at
    module load time, so it is part of the trusted publisher module chain and
    must be realpath-checked here too -- otherwise a repo-external symlink
    shadowing that evaluator would run unchecked before the publisher even
    starts. Path ancestry is decided with Path.is_relative_to() against the
    resolved project root rather than a raw str.startswith() prefix check,
    which would also treat a sibling directory such as
    "/repo-evil/..." as "under" "/repo" purely by string-prefix coincidence.
    """
    errors = []
    resolved_project_root = project_root.resolve()
    for rel in (
        _ENSURE_CONTRACT_SNAPSHOT_REL,
        _RUN_CONTRACT_REVIEW_ONCE_REL,
        _EVALUATE_PRODUCT_SPEC_GATE_REL,
        _CONTRACT_REVIEW_RESULT_PARSER_REL,
    ):
        canonical = (project_root / rel).resolve()
        if not canonical.exists():
            errors.append(f"module_missing: {rel} not found at {canonical}")
            continue
        if not canonical.is_relative_to(resolved_project_root):
            errors.append(
                f"module_shadowing: {rel} resolved to {canonical}, expected under {resolved_project_root}"
            )
    return errors


# -- Input file validation -----------------------------------------------------


def _issue_metadata_subtree(project_root: Path, issue_number: int, command_id: str) -> Path:
    """Return the canonical allowed input-file subtree for a new-style command id.

    Issue #1284: namespace is unified under
    artifacts/{issue_number}/issue-metadata/{command-id}/
    """
    return (
        project_root
        / "artifacts"
        / str(issue_number)
        / ISSUE_METADATA_NAMESPACE_SEGMENT
        / command_id
    ).resolve()


def _validate_and_resolve_input_file(
    input_file_str: str,
    issue_number: int,
    project_root: Path,
    command_id: str,
) -> tuple[Path | None, str]:
    """Validate and resolve the input file path.

    Returns (canonical_path, error_message). canonical_path is None on error.
    Enforces:
    - Lexical: reject absolute paths
    - Lexical: reject '..' components
    - Filesystem: reject symlink components (via lstat)
    - Must be a regular file
    - Must not be a hardlink (st_nlink == 1)
    - Must be under artifacts/{issue_number}/issue-metadata/{command_id}/
      (Issue #1284 command ids)
    """
    raw = PurePosixPath(input_file_str)

    # Lexical: reject absolute paths
    if raw.is_absolute():
        return None, f"input_file_absolute_path_denied: {input_file_str!r}"

    # Lexical: reject '..' components
    if ".." in raw.parts:
        return None, f"input_file_dotdot_denied: {input_file_str!r}"

    # Filesystem: check each component for symlinks via lstat
    cursor = project_root
    for part in raw.parts:
        cursor = cursor / part
        try:
            lstat = cursor.lstat()
        except FileNotFoundError:
            return None, f"input_file_not_found: {input_file_str!r}"
        except Exception as exc:  # noqa: BLE001
            return None, f"input_file_lstat_error: {exc}"
        if _stat.S_ISLNK(lstat.st_mode):
            return None, f"input_file_symlink_denied: {cursor}"

    # Resolve canonical path (no symlinks remain after lstat check above)
    try:
        canonical = cursor.resolve()
    except Exception as exc:  # noqa: BLE001
        return None, f"input_file_resolve_error: {exc}"

    # Must be a regular file
    try:
        st = canonical.stat()
    except Exception as exc:  # noqa: BLE001
        return None, f"input_file_stat_error: {exc}"

    if not _stat.S_ISREG(st.st_mode):
        return None, f"input_file_not_regular: {input_file_str!r}"

    # Hardlink check
    if st.st_nlink != 1:
        return None, f"input_file_hardlink_denied: st_nlink={st.st_nlink}"

    # Containment check.
    artifact_subtree = _issue_metadata_subtree(project_root, issue_number, command_id)
    try:
        canonical.relative_to(artifact_subtree)
    except ValueError:
        return None, (f"input_file_outside_issue_subtree: {canonical} not under {artifact_subtree}")

    return canonical, ""


# -- Input JSON validation -----------------------------------------------------


def _load_and_validate_input_json(
    canonical_input: Path, issue_number: int, command_id: str
) -> tuple[dict | None, str]:
    """Read and validate input JSON against the per-command-id schema (AC10).

    Returns (input_data, error_message). input_data is None on error.
    """
    try:
        input_data = json.loads(canonical_input.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"input_json_read_error: {exc}"

    if not isinstance(input_data, dict):
        return None, "input_json_not_object"

    expected_schema = INPUT_SCHEMA_BY_COMMAND.get(command_id)
    if input_data.get("schema") != expected_schema:
        schema_val = input_data.get("schema")
        return None, (f"input_schema_mismatch: expected {expected_schema}, got {schema_val!r}")

    input_issue = input_data.get("issue_number")
    if input_issue is None:
        return None, "input_issue_number_missing"
    if type(input_issue) is not int:
        return None, f"input_issue_number_not_int: {type(input_issue).__name__}"
    if input_issue != issue_number:
        return None, f"input_issue_number_mismatch: {input_issue} != {issue_number}"

    return input_data, ""


# -- Issue #1284: per-command input field validation ---------------------------


def _validate_issue_body_update_fields(data: dict) -> str:
    for field, typ in (
        ("previous_body_sha256", str),
        ("previous_updated_at", str),
        ("new_body", str),
        ("new_body_sha256", str),
    ):
        val = data.get(field)
        if not isinstance(val, typ) or (typ is str and not val):
            return f"issue_body_update_field_invalid: {field!r}"
    computed = "sha256:" + hashlib.sha256(data["new_body"].encode("utf-8")).hexdigest()
    if computed != data["new_body_sha256"]:
        return f"issue_body_update_new_body_sha256_mismatch: computed={computed} declared={data['new_body_sha256']}"
    return ""


_ISSUE_CONTENT_UPDATE_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "issue_number",
        "repo",
        "expected_previous_title",
        "expected_previous_body_sha256",
        "expected_previous_updated_at",
        "new_title",
        "new_body",
        "new_body_sha256",
        "operation_reason",
        "idempotency_key",
    }
)


def _validate_issue_content_update_fields(data: dict, repo: str, issue_number: int) -> str:
    unknown_keys = set(data) - _ISSUE_CONTENT_UPDATE_ALLOWED_KEYS
    if unknown_keys:
        return f"issue_content_update_unknown_fields: {sorted(unknown_keys)}"
    if data.get("repo") != repo:
        return "issue_content_update_repo_mismatch"
    if data.get("issue_number") != issue_number:
        return "issue_content_update_issue_number_mismatch"
    for field in (
        "expected_previous_title",
        "expected_previous_body_sha256",
        "expected_previous_updated_at",
        "new_title",
        "new_body",
        "new_body_sha256",
        "operation_reason",
        "idempotency_key",
    ):
        if not isinstance(data.get(field), str) or (field != "new_body" and not data[field]):
            return f"issue_content_update_field_invalid: {field!r}"
    for field in ("expected_previous_title", "new_title"):
        value = data[field]
        if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
            return f"issue_content_update_title_invalid: {field!r}"
    computed = "sha256:" + hashlib.sha256(data["new_body"].encode("utf-8")).hexdigest()
    if computed != data["new_body_sha256"]:
        return "issue_content_update_new_body_sha256_mismatch"
    return ""


def _validate_issue_comment_publish_fields(data: dict) -> str:
    for field in ("comment_body", "marker"):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            return f"issue_comment_publish_field_invalid: {field!r}"
    if data["marker"] not in data["comment_body"]:
        return "issue_comment_publish_marker_not_embedded_in_body"
    return ""


# Issue #1908: human-history comments deliberately use the existing generic
# issue_comment.publish lane.  They are distinguished only by this new marker
# namespace; existing machine-readable markers remain on the generic path.
_HUMAN_HISTORY_NAMESPACE = "loop-protocol/human-history"
_HUMAN_HISTORY_MARKER_RE = _re.compile(
    r"^<!-- loop-protocol/human-history:v1:sha256:([0-9a-f]{64}) -->$"
)


def _is_human_history_body_or_marker(comment_body: str, marker: str) -> bool:
    return _HUMAN_HISTORY_NAMESPACE in comment_body or _HUMAN_HISTORY_NAMESPACE in marker


def _parse_human_history_marker_source(body: str) -> tuple[dict | None, str]:
    """Validate and remove the one strict v1 marker from a raw comment body.

    This intentionally scans every namespace occurrence before normalizing the
    content.  A malformed namespace token is never treated as harmless prose,
    which prevents it from being hidden by digest canonicalization.
    """
    occurrences = []
    start = 0
    while True:
        pos = body.find(_HUMAN_HISTORY_NAMESPACE, start)
        if pos < 0:
            break
        occurrences.append(pos)
        start = pos + len(_HUMAN_HISTORY_NAMESPACE)
    if len(occurrences) != 1:
        return None, "human_history_marker_namespace_occurrence_count_invalid"

    pos = occurrences[0]
    opening = body.rfind("<!--", 0, pos + 1)
    closing = body.find("-->", pos)
    if opening < 0 or closing < 0:
        return None, "human_history_marker_malformed_delimiter"
    closing += 3
    token = body[opening:closing]
    match = _HUMAN_HISTORY_MARKER_RE.fullmatch(token)
    if match is None:
        return None, "human_history_marker_malformed_grammar"

    # The token itself must be the entire final non-empty line.  Only newline
    # terminators may follow it; no spaces, prose, or a second line may follow.
    line_start = max(body.rfind("\n", 0, opening), body.rfind("\r", 0, opening)) + 1
    if line_start != opening or any(ch not in "\r\n" for ch in body[closing:]):
        return None, "human_history_marker_not_final_exact_line"
    marker_line_end = closing
    # Removing the exact marker line leaves its terminal line terminator for
    # normal content-digest terminal-LF stripping, as required by the contract.
    without_marker = body[:opening] + body[marker_line_end:]
    normalized = without_marker.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    digest = hashlib.sha256(normalized.encode("utf-8", "strict")).hexdigest()
    return {
        "marker": token,
        "identity_sha256": match.group(1),
        "content_digest": digest,
        "content_without_marker": without_marker,
    }, ""


_PR_HEAD_SHA_RE = _re.compile(r"^[0-9a-f]{40}$")

# Issue #1647: this transaction is deliberately distinct from the generic
# issue-comment publisher.  The CLI issue number is the linked Issue; the PR
# is a separate, mandatory field and is never inferred from it.
_TEST_VERDICT_PUBLISH_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "issue_number",
        "repo",
        "target_pr_number",
        "linked_issue_number",
        "expected_head_sha",
        "linked_issue_body_sha256",
        "producer_receipt",
        "receipt_sha256",
        "idempotency_key",
    }
)
# Issue #1647 Scope Delta AC8: the published body is never accepted as
# free-form caller input. It is rendered deterministically from the
# verified receipt inside the executor (see _render_test_verdict_body).
_TEST_VERDICT_BODY_MAX_BYTES = 60000
# Issue #1647 Scope Delta AC5: read-only reference to the locked receipt
# schema. This file is never written by test_verdict.publish.
_TEST_VERDICT_RECEIPT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "test-verdict-producer-receipt.schema.json"
)
TEST_VERDICT_MARKER_PREFIX = "<!-- TEST_VERDICT_PUBLISH_MARKER:"
TEST_VERDICT_MARKER_SUFFIX = " -->"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _test_verdict_marker_str(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"{TEST_VERDICT_MARKER_PREFIX}{digest}{TEST_VERDICT_MARKER_SUFFIX}"


def _validate_producer_receipt_schema(receipt: dict) -> str:
    """Issue #1647 Scope Delta AC5: full-schema fail-closed validation of
    ``producer_receipt`` against the locked TEST_VERDICT_PRODUCER_RECEIPT_V1
    schema (``required`` + ``additionalProperties: false``, including the
    ``producer`` object and ``execution_payload_sha256`` that the previous
    partial hand-rolled checks never enforced). Mirrors the
    ``_validate_against_schema`` pattern in
    scripts/agent-ops/test_verdict_execution_record_producer.py."""
    try:
        from jsonschema import Draft202012Validator
    except Exception as exc:  # noqa: BLE001
        return f"test_verdict_publish_receipt_schema_validator_unavailable: {exc}"
    try:
        schema = json.loads(_TEST_VERDICT_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return f"test_verdict_publish_receipt_schema_file_unreadable: {exc}"
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(receipt))
    if errors:
        messages = " and ".join(str(e) for e in errors[:5])
        return f"test_verdict_publish_receipt_schema_invalid: {messages}"
    return ""


def _validate_test_verdict_publish_fields(data: dict, repo: str, issue_number: int) -> str:
    unknown_keys = set(data.keys()) - _TEST_VERDICT_PUBLISH_ALLOWED_KEYS
    if unknown_keys:
        return f"test_verdict_publish_unknown_fields: {sorted(unknown_keys)}"
    if data.get("repo") != repo:
        return "test_verdict_publish_repo_mismatch"
    if data.get("linked_issue_number") != issue_number or data.get("issue_number") != issue_number:
        return "test_verdict_publish_linked_issue_number_mismatch"
    target_pr_number = data.get("target_pr_number")
    if (
        type(target_pr_number) is not int
        or target_pr_number <= 0
        or target_pr_number == issue_number
    ):
        return "test_verdict_publish_target_pr_number_invalid"
    expected_head_sha = data.get("expected_head_sha")
    if not isinstance(expected_head_sha, str) or not _PR_HEAD_SHA_RE.match(expected_head_sha):
        return "test_verdict_publish_expected_head_sha_invalid"
    linked_issue_body_sha256 = data.get("linked_issue_body_sha256")
    if not isinstance(linked_issue_body_sha256, str) or not _re.fullmatch(
        r"sha256:[0-9a-f]{64}", linked_issue_body_sha256
    ):
        return "test_verdict_publish_linked_issue_body_sha256_invalid"
    receipt = data.get("producer_receipt")
    if not isinstance(receipt, dict):
        return "test_verdict_publish_receipt_invalid"
    schema_err = _validate_producer_receipt_schema(receipt)
    if schema_err:
        return schema_err
    if receipt.get("pass_eligible") is not True:
        return "test_verdict_publish_receipt_not_pass_eligible"
    subject = receipt.get("subject")
    contract = receipt.get("contract")
    artifact = receipt.get("execution_artifact")
    if (
        not isinstance(subject, dict)
        or subject.get("target_pr_number") != target_pr_number
        or subject.get("pr_head_sha") != expected_head_sha
    ):
        return "test_verdict_publish_receipt_subject_mismatch"
    if (
        not isinstance(contract, dict)
        or contract.get("linked_issue_number") != issue_number
        or contract.get("issue_body_sha256") != linked_issue_body_sha256
    ):
        return "test_verdict_publish_receipt_contract_mismatch"
    # Issue 1711 Blocker 3: the receipt's trusted source binding (repository
    # id/full_name/commit sha/tree sha, independently resolved by the
    # trusted-receipt job via GitHub REST API, never from the untrusted PR
    # checkout) must cross-check against this same publish transaction's own
    # repo/expected_head_sha, in addition to the schema-level required-field
    # validation already performed above.
    source = receipt.get("source")
    if (
        not isinstance(source, dict)
        or source.get("repository_full_name") != repo
        or source.get("commit_sha") != expected_head_sha
        or not isinstance(source.get("repository_id"), int)
        or source.get("repository_id") <= 0
        or not isinstance(source.get("tree_sha"), str)
        or not _re.fullmatch(r"[0-9a-f]{40}", source.get("tree_sha") or "")
        or not isinstance(source.get("execution_run_id"), int)
        or source.get("execution_run_id") <= 0
        or not isinstance(source.get("execution_job_id"), int)
        or source.get("execution_job_id") <= 0
    ):
        return "test_verdict_publish_receipt_source_mismatch"
    if (
        not isinstance(artifact, dict)
        or not isinstance(artifact.get("artifact_id"), int)
        or artifact["artifact_id"] <= 0
        or not isinstance(artifact.get("artifact_url"), str)
        or not artifact["artifact_url"]
        or not isinstance(artifact.get("artifact_archive_digest"), str)
        or not _re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["artifact_archive_digest"])
    ):
        return "test_verdict_publish_receipt_artifact_invalid"
    receipt_sha256 = data.get("receipt_sha256")
    if receipt_sha256 != _canonical_sha256(receipt):
        return "test_verdict_publish_receipt_sha256_mismatch"
    # Issue #1647 Scope Delta AC8: the body is never accepted as free-form
    # caller input -- it is rendered deterministically from the verified
    # receipt (see _render_test_verdict_body), so the idempotency key no
    # longer binds a caller-supplied body_sha256.
    expected_key = f"{repo}:{target_pr_number}:{issue_number}:{expected_head_sha}:{receipt_sha256}"
    if data.get("idempotency_key") != expected_key:
        return "test_verdict_publish_idempotency_key_mismatch"
    return ""


def _validate_contract_snapshot_publish_fields(data: dict, repo: str) -> str:
    """Issue #1284 Blocker 4: CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1 must bind repo /
    target_issue_body_sha256 / expected_latest_contract_review_status /
    expected_contract_marker / operation_reason. An input file with only
    {schema, issue_number} is no longer sufficient to launch
    ensure_contract_snapshot.py --mode auto --post.
    """
    declared_repo = data.get("repo")
    if declared_repo != repo:
        return f"contract_snapshot_publish_repo_mismatch: {declared_repo!r} != {repo!r}"
    for field in (
        "target_issue_body_sha256",
        "expected_latest_contract_review_status",
        "expected_contract_marker",
        "operation_reason",
    ):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            return f"contract_snapshot_publish_field_invalid: {field!r}"
    return ""


def _validate_issue_scope_snapshot_materialize_fields(data: dict, repo: str) -> str:
    if data.get("repo") != repo:
        return "issue_scope_snapshot_materialize_repo_mismatch"
    for field in (
        "contract_snapshot_url",
        "base_ref",
        "branch_name",
        "worktree_path",
        "output_path",
    ):
        if not isinstance(data.get(field), str) or not data[field]:
            return f"issue_scope_snapshot_materialize_field_invalid: {field!r}"
    return ""


# -- Issue #1284: env binding (AC15) --------------------------------------------


def _check_issue_env_binding(command_id: str, issue_number: int) -> str:
    """Return error string, or empty string when binding is satisfied.

    LOOP_ISSUE_NUMBER is optional for every command id; when present it must
    match --issue-number (Issue #1284 AC15). ENV_BINDING_MANDATORY_COMMAND_IDS
    is currently empty (Issue #1873 removed its only member,
    termination_report.publish) but is kept as an extension point.
    """
    env_issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    mandatory = command_id in ENV_BINDING_MANDATORY_COMMAND_IDS
    if not env_issue:
        if mandatory:
            return "loop_issue_number_env_missing: LOOP_ISSUE_NUMBER must be set"
        return ""
    if not env_issue.isdigit():
        return f"loop_issue_number_env_not_digit: {env_issue!r}"
    if int(env_issue) != issue_number:
        return (
            f"issue_number_mismatch: --issue-number {issue_number} != LOOP_ISSUE_NUMBER {env_issue}"
        )
    return ""


# -- Issue #1284: HTTP error classification (same granularity as
# ensure_contract_snapshot.py's classify_post_http_error / _extract_http_status)


def _extract_http_status(stderr: str) -> int | None:
    """Extract HTTP status code from gh CLI stderr."""
    m = _re.search(r"HTTP (\d{3})", stderr or "")
    if m:
        return int(m.group(1))
    for code in (403, 404, 410, 422, 429, 503):
        if str(code) in (stderr or ""):
            return code
    return None


# Issue #2340 AC5: actor capability artifact / worklog sanitization. `gh`
# does not normally echo credential values into stderr, but this is a
# defense-in-depth belt for the one branch below (`_classify_gh_error`'s
# unknown-pattern fallback) that ever includes ANY caller-observed process
# output in its return value -- every other branch here already returns a
# fixed, credential-free canonical string. Redaction runs BEFORE bounding, so
# a token that happens to fall within the truncation window never survives.
#
# Issue #2718: GitHub's 2026-04-24 changelog introduced a new-format GitHub
# App installation token shape: `ghs_<APP_ID>_<JWT>` (the `ghs_` prefix is
# unchanged; the token body is APP_ID followed by a JWT in compact
# serialization -- header, payload, and signature segments, each base64url
# encoded, joined by `.` delimiters). Note that `.` is the JWS
# compact-serialization segment separator, not itself a base64url character
# -- base64url's own charset is `[A-Za-z0-9_-]`. Including `.` in the char
# class below is a deliberate, simple extension so one pattern can match the
# whole dot-joined token in a single pass; it does not imply `.` is part of
# base64url, and this pattern does not parse or rely on the JWT's internal
# header/payload/signature semantics. New-format tokens run to ~520 chars
# total, so the pattern requires at least 36 chars after the `ghs_` prefix --
# wide enough to match real new-format tokens (and external prior art such as
# github/gh-aw-firewall PR #3786 and aquasecurity/trivy PR #10826, which both
# use `{36,}`) while narrowing false-positive redaction of short
# punctuation-bearing strings that happen to start with `ghs_`.
#
# On a representative new-format fixture (a ~9-digit APP_ID), the
# pre-existing `gh[oprsu]_[A-Za-z0-9]{20,}` pattern below does not match at
# all: its body class is alphanumeric-only, and a 9-digit APP_ID is far
# short of the 20-char minimum, so the match attempt fails outright at the
# first `_` separator rather than partially consuming the token. This new
# pattern is listed FIRST only so `_redact_secret_like_tokens()` (which
# applies patterns sequentially via `.sub()`) prefers the more specific
# `ghs_`-aware pattern before the general `gh[oprsu]_` pattern gets a chance
# to look at the same text; on today's fixtures the general pattern would
# not have matched the new-format token regardless of ordering, so this
# ordering is a specific-before-general convention, not a correctness
# requirement for the fixtures currently in this test suite.
_SECRET_LIKE_PATTERNS = (
    _re.compile(r"ghs_[A-Za-z0-9._-]{36,}"),
    _re.compile(r"gh[oprsu]_[A-Za-z0-9]{20,}"),
    _re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    _re.compile(r"(?i)\bauthorization:\s*bearer\s+\S+"),
    _re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}"),
)


def _redact_secret_like_tokens(text: str) -> str:
    """Redact GitHub-token-shaped and Authorization-header-shaped
    substrings from a diagnostic string before it is ever bounded/returned
    (Issue #2340 AC5)."""
    redacted = text
    for pattern in _SECRET_LIKE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _classify_gh_error(prefix: str, stderr: str) -> str:
    """Classify a gh api failure into a deterministic error code.

    403 -> permission_denied, 404/410 -> ambiguous_no_retry, 422 -> validation_failed,
    429/503 -> rate_limited, unknown -> the redacted, truncated stderr (Issue
    #2340 AC5: reason_code / bounded sanitized diagnostic only -- never a raw
    credential/token value, even in this fallback branch).
    """
    status = _extract_http_status(stderr)
    if status == 403:
        return f"{prefix}_permission_denied_http_403"
    if status in (404, 410):
        return f"{prefix}_ambiguous_no_retry_http_{status}"
    if status == 422:
        return f"{prefix}_validation_failed_http_422"
    if status in (429, 503):
        return f"{prefix}_rate_limited_http_{status}"
    return f"{prefix}: {_redact_secret_like_tokens(stderr.strip())[:200]}"


# -- Issue #1284: issue body / comment mutation helpers ------------------------


def _fetch_issue_content(issue_number: int, repo: str, gh_bin: str) -> tuple[dict | None, str]:
    """Read the fixed Issue endpoint without inheriting caller gh settings."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/{issue_number}",
                "--jq",
                '{title, body, updatedAt: .updated_at, isPullRequest: has("pull_request")}',
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=_build_metadata_sanitized_env(),
        )
        if out.returncode != 0:
            return None, f"gh_issue_fetch_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        if (
            not isinstance(data, dict)
            or not (isinstance(data.get("body"), str) or data.get("body") is None)
            or not isinstance(data.get("updatedAt"), str)
            or not isinstance(data.get("isPullRequest"), bool)
        ):
            return None, "gh_issue_fetch_schema_invalid"
        data["body"] = data["body"] or ""
        # Existing body-only consumers use the same fixed endpoint but do not
        # require a title fixture. ``issue_content.update`` enforces title
        # presence at its own pre/postcondition boundary.
        if "title" not in data:
            data["title"] = None
        return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_issue_fetch_exception: {exc}"


def _fetch_issue_body_and_updated_at(
    issue_number: int, repo: str, gh_bin: str
) -> tuple[str | None, str | None, str]:
    """Fetch live Issue state from the trusted GitHub host only.

    ``contract_snapshot.publish`` uses this helper for both its stale-write
    precondition and its post-publish live-body revalidation.  Those reads are
    part of the authoritative success boundary, so they must not inherit a
    caller-controlled GH_HOST/GH_REPO override (Issue #2340 fix_delta P0-1:
    GH_CONFIG_DIR / GH_TOKEN / GITHUB_TOKEN are the intentional credential
    carrier and are not stripped -- see `_build_metadata_sanitized_env()`).
    """
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/{issue_number}",
                "--jq",
                "{body, updatedAt: .updated_at}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=_build_metadata_sanitized_env(),
        )
        if out.returncode != 0:
            return None, None, f"gh_issue_fetch_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        return data.get("body", ""), data.get("updatedAt", ""), ""
    except Exception as exc:  # noqa: BLE001
        return None, None, f"gh_issue_fetch_exception: {exc}"


def _patch_issue_body(issue_number: int, repo: str, new_body: str, gh_bin: str) -> str:
    """PATCH issue body via gh api argv-list (no gh issue edit CLI). Returns error or ''.

    Issue #2340 AC1 (P0 credential parity): this write path previously
    inherited the caller's ambient environment while the sibling read helpers
    (`_fetch_issue_content` / `_fetch_issue_body_and_updated_at`) explicitly
    passed `env=_build_metadata_sanitized_env()`. That asymmetry let a read
    and a write in the same logical transaction execute under different
    GitHub credential/host contexts. This now pins the trusted host and
    sanitized env identically to the read helpers.
    """
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(new_body)
            tmp_path = tmp.name
        try:
            out = subprocess.run(  # noqa: PLW1510
                [
                    gh_bin,
                    "api",
                    "--hostname",
                    _TRUSTED_GITHUB_HOST,
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/{issue_number}",
                    "--field",
                    f"body=@{tmp_path}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
                env=_build_metadata_sanitized_env(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out.returncode != 0:
            return _classify_gh_error("gh_api_patch_failed", out.stderr or "")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"gh_api_patch_exception: {exc}"


def _patch_issue_content(issue_number: int, repo: str, title: str, body: str, gh_bin: str) -> str:
    """Send title and body in exactly one fixed REST PATCH request."""
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump({"title": title, "body": body}, tmp, ensure_ascii=False)
            tmp_path = tmp.name
        try:
            out = subprocess.run(  # noqa: PLW1510
                [
                    gh_bin,
                    "api",
                    "--hostname",
                    _TRUSTED_GITHUB_HOST,
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/{issue_number}",
                    "--input",
                    tmp_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
                env=_build_metadata_sanitized_env(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out.returncode != 0:
            return _classify_gh_error("gh_api_patch_failed", out.stderr or "")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"gh_api_patch_exception: {exc}"


def _post_gh_comment(issue_number: int, repo: str, body: str, gh_bin: str) -> tuple[str, str, str]:
    """POST a comment via gh api argv-list. Returns (comment_url, comment_id, error).

    Issue #2340 AC1: same-transaction issue comment publish route -- pinned to
    the trusted host and sanitized env for parity with the issue body
    read/write helpers above (previously inherited ambient env unsanitized).
    """
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        try:
            out = subprocess.run(  # noqa: PLW1510
                [
                    gh_bin,
                    "api",
                    "--hostname",
                    _TRUSTED_GITHUB_HOST,
                    "--method",
                    "POST",
                    f"repos/{repo}/issues/{issue_number}/comments",
                    "--field",
                    f"body=@{tmp_path}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
                env=_build_metadata_sanitized_env(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out.returncode != 0:
            return "", "", _classify_gh_error("gh_api_post_comment_failed", out.stderr or "")
        try:
            resp = json.loads(out.stdout)
        except Exception as exc:  # noqa: BLE001
            return "", "", f"gh_api_post_comment_response_parse_error: {exc}"
        return str(resp.get("html_url", "")), str(resp.get("id", "")), ""
    except Exception as exc:  # noqa: BLE001
        return "", "", f"gh_api_post_comment_exception: {exc}"


# -- Postcondition check -------------------------------------------------------
#
# Issue #2163 review fix_delta (PR #2178 REQUEST_CHANGES): the previous
# metadata-snapshot design had multiple reproducible fail-open bypasses. This
# section replaces it with three separated responsibilities, matching the
# reviewer-recommended structure:
#
#   _collect_repo_state()        -- structured `git status --porcelain=v2 -z`
#                                    index state + exact filesystem node/content
#                                    identity for every non-allowed candidate.
#   _check_clean_precondition()  -- current-state gate used before a remote
#                                    mutation begins (returns the captured
#                                    state too, so it doubles as the
#                                    pre-mutation snapshot without a second
#                                    `git status` invocation).
#   _compare_repo_transition()   -- before/after union-based transition
#                                    classification (Issue #2163 P0-2/P0-3).
#
# `_capture_pre_mutation_snapshot` / `_check_no_tracked_changes` remain as the
# public entry points every `_run_*` command handler calls, now implemented
# in terms of the three primitives above.


class _AbsentMarker:
    """Sentinel: this path was affirmatively confirmed not to exist
    (`FileNotFoundError`) at capture time. Distinct from `_UNOBSERVED` (Issue
    #2163 P0-2 deletion_absence_sentinel_collision)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debug aid only
        return "<ABSENT>"


class _UnobservedMarker:
    """Sentinel: this path was never a `git status` candidate in this
    snapshot (neither observed present nor confirmed absent). Distinct from
    `_ABSENT` (Issue #2163 P0-2)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debug aid only
        return "<UNOBSERVED>"


_ABSENT = _AbsentMarker()
_UNOBSERVED = _UnobservedMarker()


class _RepoStateCaptureError(Exception):
    """Raised when repo state (git status v2 -z parse, or filesystem node
    identity) cannot be captured with certainty. Issue #2163 P1-1
    (ambiguous_oserror_treated_as_absent): any `OSError` other than
    `FileNotFoundError` raises this instead of being folded into `_ABSENT`,
    so the caller fails closed (baseline capture failure / postcondition
    indeterminate) rather than silently treating an EACCES/EIO/ENOTDIR node
    as though it does not exist."""


class _FsNodeState(NamedTuple):
    """Exact filesystem node identity for one path (Issue #2163 P1-1).

    Regular files carry a SHA-256 content digest (not just mtime/size, which
    cannot distinguish a same-size content rewrite with a restored mtime).
    Symlinks carry their `readlink()` target. `st_mode`/`st_dev`/`st_ino`/
    `st_nlink`/`st_ctime_ns` are preserved so mode-only changes (chmod),
    hardlink-count changes, and atomic-replace inode changes are all visible
    in equality comparison, matching `skill_runtime_exec.py`'s
    `_snapshot_repo_paths` design intent."""

    node_type: str  # "regular" | "symlink" | "dir" | "fifo" | "socket" | "block_device" | "char_device" | "other"
    st_mode: int
    st_dev: int
    st_ino: int
    st_nlink: int
    st_ctime_ns: int
    st_mtime_ns: int
    st_size: int
    content_sha256: str | None
    symlink_target: str | None


def _stat_fs_node(abs_path: Path) -> _FsNodeState | _AbsentMarker:
    """Return the exact node identity for `abs_path`, `_ABSENT` if it
    genuinely does not exist, or raise `_RepoStateCaptureError` for any other
    `OSError` (Issue #2163 P1-1 fail-closed error semantics)."""
    try:
        st = abs_path.lstat()
    except FileNotFoundError:
        return _ABSENT
    except OSError as exc:
        raise _RepoStateCaptureError(f"lstat_failed: {abs_path}: {exc}") from exc

    mode = st.st_mode
    content_sha256: str | None = None
    symlink_target: str | None = None
    if _stat.S_ISREG(mode):
        node_type = "regular"
        try:
            with open(abs_path, "rb") as fh:
                content_sha256 = hashlib.file_digest(fh, "sha256").hexdigest()
        except FileNotFoundError:
            return _ABSENT
        except OSError as exc:
            raise _RepoStateCaptureError(f"content_digest_failed: {abs_path}: {exc}") from exc
    elif _stat.S_ISLNK(mode):
        node_type = "symlink"
        try:
            symlink_target = os.readlink(abs_path)
        except FileNotFoundError:
            return _ABSENT
        except OSError as exc:
            raise _RepoStateCaptureError(f"readlink_failed: {abs_path}: {exc}") from exc
    elif _stat.S_ISDIR(mode):
        node_type = "dir"
    elif _stat.S_ISFIFO(mode):
        node_type = "fifo"
    elif _stat.S_ISSOCK(mode):
        node_type = "socket"
    elif _stat.S_ISBLK(mode):
        node_type = "block_device"
    elif _stat.S_ISCHR(mode):
        node_type = "char_device"
    else:
        node_type = "other"  # pragma: no cover -- exotic node type

    return _FsNodeState(
        node_type=node_type,
        st_mode=mode,
        st_dev=st.st_dev,
        st_ino=st.st_ino,
        st_nlink=st.st_nlink,
        st_ctime_ns=st.st_ctime_ns,
        st_mtime_ns=st.st_mtime_ns,
        st_size=st.st_size,
        content_sha256=content_sha256,
        symlink_target=symlink_target,
    )


class _PorcelainEntry(NamedTuple):
    """One `git status --porcelain=v2 -z` record (Issue #2163 P0-3). Carries
    the `XY` code plus HEAD/index/worktree mode and HEAD/index object id so a
    staged/unstaged/untracked transition is detectable even when filesystem
    metadata is unchanged (e.g. `git add` on an already-identical file)."""

    xy: str
    sub: str
    mode_head: str | None
    mode_index: str | None
    mode_worktree: str | None
    hash_head: str | None
    hash_index: str | None
    orig_path: str | None  # rename/copy source path (Issue #2163 P0-1), else None


def _run_git_status_v2_z(project_root: Path) -> tuple[dict[str, _PorcelainEntry], str | None]:
    """Run `git status --porcelain=v2 -z --untracked-files=all --renames` and
    parse its NUL-delimited output into structured records (Issue #2163
    P0-1: replaces the previous `--porcelain=v1` + `text=True` +
    `splitlines()` + `line[3:]` parser, which does not decode C-style quoted
    pathnames -- spaces/tabs/newlines/quotes/backslashes -- and does not
    split a rename's two pathnames apart). Any parse failure returns an
    explicit error instead of silently dropping records (fail-closed)."""
    try:
        proc = subprocess.run(  # noqa: PLW1510
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain=v2",
                "-z",
                "--untracked-files=all",
                "--renames",
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {}, f"git_status_v2_exception: {exc}"

    # Defensive: `subprocess.run(..., capture_output=True)` without
    # `text=True` always yields `bytes` for stdout/stderr in real execution
    # -- this shape is unreachable via a genuine git invocation. A non-bytes
    # `proc` here can only occur when something outside this function has
    # substituted a different `subprocess.run` return value for an unrelated
    # purpose; treat it as "no git status candidates observed" rather than
    # raising, since real git output never takes this shape.
    if not isinstance(proc.stdout, (bytes, bytearray)) or not isinstance(
        proc.stderr, (bytes, bytearray)
    ):
        return {}, None

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:200]
        return {}, f"git_status_v2_failed: {stderr}"

    tokens = proc.stdout.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()

    def _dec(raw: bytes) -> str:
        return raw.decode("utf-8", "surrogateescape")

    entries: dict[str, _PorcelainEntry] = {}
    i = 0
    n = len(tokens)
    while i < n:
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        kind = rec[:1]
        try:
            if kind == b"1":
                parts = rec.split(b" ", 8)
                if len(parts) != 9:
                    return {}, f"git_status_v2_malformed_ordinary_record: {rec!r}"
                _, xy, sub, mh, mi, mw, hh, hi, path_b = parts
                path = _dec(path_b)
                entries[path] = _PorcelainEntry(
                    xy=_dec(xy),
                    sub=_dec(sub),
                    mode_head=_dec(mh),
                    mode_index=_dec(mi),
                    mode_worktree=_dec(mw),
                    hash_head=_dec(hh),
                    hash_index=_dec(hi),
                    orig_path=None,
                )
            elif kind == b"2":
                parts = rec.split(b" ", 9)
                if len(parts) != 10:
                    return {}, f"git_status_v2_malformed_rename_record: {rec!r}"
                _, xy, sub, mh, mi, mw, hh, hi, _score, path_b = parts
                path = _dec(path_b)
                if i >= n:
                    return {}, "git_status_v2_missing_orig_path_field"
                orig_path = _dec(tokens[i])
                i += 1
                entries[path] = _PorcelainEntry(
                    xy=_dec(xy),
                    sub=_dec(sub),
                    mode_head=_dec(mh),
                    mode_index=_dec(mi),
                    mode_worktree=_dec(mw),
                    hash_head=_dec(hh),
                    hash_index=_dec(hi),
                    orig_path=orig_path,
                )
            elif kind == b"u":
                parts = rec.split(b" ", 10)
                if len(parts) != 11:
                    return {}, f"git_status_v2_malformed_unmerged_record: {rec!r}"
                _, xy, sub, m1, m2, m3, mw, h1, h2, h3, path_b = parts
                path = _dec(path_b)
                entries[path] = _PorcelainEntry(
                    xy=_dec(xy),
                    sub=_dec(sub),
                    mode_head=_dec(m1),
                    mode_index=_dec(m2) + ":" + _dec(m3),
                    mode_worktree=_dec(mw),
                    hash_head=_dec(h1),
                    hash_index=_dec(h2) + ":" + _dec(h3),
                    orig_path=None,
                )
            elif kind == b"?":
                parts = rec.split(b" ", 1)
                if len(parts) != 2:
                    return {}, f"git_status_v2_malformed_untracked_record: {rec!r}"
                path = _dec(parts[1])
                entries[path] = _PorcelainEntry(
                    xy="??",
                    sub="",
                    mode_head=None,
                    mode_index=None,
                    mode_worktree=None,
                    hash_head=None,
                    hash_index=None,
                    orig_path=None,
                )
            elif kind == b"!":
                continue  # ignored entries not requested (no --ignored flag), defensive only
            else:
                return {}, f"git_status_v2_unknown_record_kind: {rec[:1]!r}"
        except (UnicodeDecodeError, ValueError) as exc:
            return {}, f"git_status_v2_parse_error: {exc}"
    return entries, None


class _PathObservation(NamedTuple):
    """Combined git-index-state + filesystem-node-identity observation for
    one candidate path within a single `_RepoState` snapshot (Issue #2163
    P0-3). `xy is None` means this path had no `git status` line of its own
    in this snapshot (it is only present because it is the rename/copy
    *source* side of a sibling entry)."""

    xy: str | None
    sub: str | None
    mode_head: str | None
    mode_index: str | None
    mode_worktree: str | None
    hash_head: str | None
    hash_index: str | None
    orig_path: str | None
    fs: _FsNodeState | _AbsentMarker


class _RepoState(NamedTuple):
    """A single point-in-time `git status --porcelain=v2 -z` + filesystem
    snapshot, scoped to the non-allowed candidate paths (Issue #2163)."""

    candidate_paths: frozenset[str]
    observations: dict[str, _PathObservation]


def _collect_repo_state(
    project_root: Path, issue_number: int, allowed_prefix: str | None
) -> _RepoState:
    """Capture structured `git status --porcelain=v2 -z` index state and
    exact filesystem node identity for every candidate path not fully
    contained in `allowed_prefix` (Issue #2163 recommended
    `collect_repo_state()` primitive).

    A rename/copy entry is only treated as "fully allowed" (excluded from
    the candidate set) when *both* its destination and its source path are
    within `allowed_prefix` -- otherwise both endpoints are added as
    individually-authorized candidates (Issue #2163 P0-1: a rename with only
    one endpoint inside the allowed root must not be silently excluded).

    Raises `_RepoStateCaptureError` on any `git status` parse failure or any
    filesystem stat/read failure that is not a confirmed absence -- this
    propagates to callers so pre-mutation capture failures stop the command
    *before* any remote mutation is attempted (Issue #2163 P1-1).
    """
    if allowed_prefix is None:
        allowed_prefix = f"artifacts/{issue_number}/"

    entries, err = _run_git_status_v2_z(project_root)
    if err is not None:
        raise _RepoStateCaptureError(err)

    def _within_allowed(rel: str) -> bool:
        return rel.startswith(allowed_prefix)

    candidate_paths: set[str] = set()
    entry_by_candidate: dict[str, _PorcelainEntry] = {}
    for path, entry in entries.items():
        fully_allowed = _within_allowed(path) and (
            entry.orig_path is None or _within_allowed(entry.orig_path)
        )
        if fully_allowed:
            continue
        candidate_paths.add(path)
        entry_by_candidate[path] = entry
        if entry.orig_path is not None:
            candidate_paths.add(entry.orig_path)
            entry_by_candidate.setdefault(entry.orig_path, entry)

    observations: dict[str, _PathObservation] = {}
    for rel in candidate_paths:
        entry = entry_by_candidate.get(rel)
        fs = _stat_fs_node(project_root / rel)  # may raise _RepoStateCaptureError -- propagate
        observations[rel] = _PathObservation(
            xy=entry.xy if entry is not None else None,
            sub=entry.sub if entry is not None else None,
            mode_head=entry.mode_head if entry is not None else None,
            mode_index=entry.mode_index if entry is not None else None,
            mode_worktree=entry.mode_worktree if entry is not None else None,
            hash_head=entry.hash_head if entry is not None else None,
            hash_index=entry.hash_index if entry is not None else None,
            orig_path=entry.orig_path if entry is not None else None,
            fs=fs,
        )

    return _RepoState(candidate_paths=frozenset(candidate_paths), observations=observations)


def _observation_display_xy(obs: _PathObservation | None) -> str:
    if obs is not None and obs.xy:
        return obs.xy
    return "??"


def _check_clean_precondition(
    project_root: Path, issue_number: int, allowed_prefix: str | None = None
) -> tuple[_RepoState | None, list[str]]:
    """Current-state gate used before a remote mutation begins (Issue #2163
    recommended `check_clean_precondition()` primitive). Returns
    `(repo_state, violations)`. `repo_state` is `None` only when capture
    itself failed (`violations` then contains the failure reason,
    fail-closed). When `repo_state` is not `None`, it can be reused directly
    as the pre-mutation snapshot for `_compare_repo_transition` without a
    second `git status` invocation."""
    try:
        state = _collect_repo_state(project_root, issue_number, allowed_prefix)
    except _RepoStateCaptureError as exc:
        return None, [f"repo_state_capture_failed: {exc}"]
    violations = sorted(
        f"{_observation_display_xy(state.observations.get(p))}:{p}" for p in state.candidate_paths
    )
    return state, violations


def _compare_repo_transition(before: _RepoState, after: _RepoState) -> list[str]:
    """Union-based before/after transition classification (Issue #2163
    recommended `compare_repo_transition()` primitive, fixes P0-2). Compares
    `before.candidate_paths | after.candidate_paths` (not just the
    post-mutation candidate set) so a path that was a candidate before the
    mutation but disappeared entirely from `git status` afterward (e.g. a
    pre-existing untracked file that was deleted, which leaves no trace in
    `git status` output at all) is still flagged -- `_UNOBSERVED` in one
    snapshot and a real observation in the other is always a violation."""
    violations: list[str] = []
    for path in sorted(before.candidate_paths | after.candidate_paths):
        before_obs = before.observations.get(path, _UNOBSERVED)
        after_obs = after.observations.get(path, _UNOBSERVED)
        if before_obs == after_obs:
            continue
        display_obs = after_obs if after_obs is not _UNOBSERVED else before_obs
        xy = _observation_display_xy(
            display_obs if isinstance(display_obs, _PathObservation) else None
        )
        violations.append(f"{xy}:{path}")
    return violations


def _capture_pre_mutation_snapshot(
    project_root: Path, issue_number: int, allowed_prefix: str | None = None
) -> tuple[_RepoState | None, str | None]:
    """Capture the pre-mutation repo state (Issue #2163). Returns
    `(repo_state, error)`. On failure `repo_state` is `None` and `error`
    describes why -- callers MUST check for this and stop *before* any
    local/remote mutation is attempted (Issue #2163 P1-1: replaces the
    previous `except Exception: return {}` fallback, which silently treated
    a capture failure as an empty/trivially-satisfied snapshot)."""
    try:
        return _collect_repo_state(project_root, issue_number, allowed_prefix), None
    except _RepoStateCaptureError as exc:
        return None, str(exc)


def _check_no_tracked_changes(
    project_root: Path,
    issue_number: int,
    allowed_prefix: str | None = None,
    before_snapshot: _RepoState | None = None,
) -> list[str]:
    """Return list of violations (staged, unstaged, untracked, renamed,
    deleted, mode-changed, or content-changed candidate paths). Empty = OK
    (AC14).

    Uses `_collect_repo_state` (Issue #2163: `git status --porcelain=v2 -z`
    structural parse + exact filesystem node identity, see module docstring
    above) to capture the current repo state, then (when `before_snapshot`
    is provided) applies `_compare_repo_transition` against it -- comparing
    the *union* of before/after candidate paths, not just the paths present
    in the current snapshot, and comparing full index + filesystem node
    identity (not just kind/mtime/size) -- instead of treating bare presence
    in a single post-mutation `git status` line set as sufficient evidence.

    Allows writes inside allowed_prefix. Defaults to artifacts/{issue_number}/.
    Issue #1284 Blocker 6: command ids pass a command-id-scoped prefix
    (artifacts/{issue_number}/issue-metadata/{command_id}/) so the postcondition
    cannot be satisfied by writes to a sibling command's namespace.

    `before_snapshot=None` is the current-state-only gate (equivalent to
    `_check_clean_precondition`'s violations list): every remaining candidate
    is treated as a violation, matching the previous fail-closed behavior for
    callers that have not yet been threaded with a captured pre-mutation
    snapshot.
    """
    try:
        after_state = _collect_repo_state(project_root, issue_number, allowed_prefix)
    except _RepoStateCaptureError as exc:
        return [f"repo_state_capture_failed: {exc}"]

    if before_snapshot is None:
        return sorted(
            f"{_observation_display_xy(after_state.observations.get(p))}:{p}"
            for p in after_state.candidate_paths
        )

    return _compare_repo_transition(before_snapshot, after_state)


# -- Main executor -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controlled skill mutation executor (Issue #1166 / #1284)"
    )
    parser.add_argument("--command-id", required=True, help="Command ID")
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub issue number")
    parser.add_argument(
        "--input-file",
        default=None,
        help="Relative path to input JSON file (artifact subtree)",
    )
    parser.add_argument("--repo", required=True, help="GitHub repo slug (owner/repo)")
    parser.add_argument("--json", dest="output_json", action="store_true", help="JSON output")
    parser.add_argument("--dry-run", action="store_true", help="Validate but do not publish")
    args = parser.parse_args(argv)

    def _fail(
        reason: str,
        errors: list[str] | None = None,
        status: str = "error",
        extra: dict | None = None,
    ) -> int:
        result = {
            "schema": RESULT_SCHEMA,
            "status": status,
            "command_id": args.command_id,
            "reason": reason,
            "errors": errors or [reason],
        }
        if extra:
            result.update(extra)
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[controlled_skill_mutation_exec] {status}: {reason}", file=sys.stderr)
        return 2 if status == "error" else 1

    def _ok(extra: dict) -> int:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
            "repo": args.repo,
        }
        result.update(extra)
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"[controlled_skill_mutation_exec] ok: {args.command_id} issue #{args.issue_number}",
                file=sys.stderr,
            )
        return 0

    # -- AC10 / AC8: validate command_id (unknown command id → exit 2) --------
    if args.command_id not in ALL_COMMAND_IDS:
        return _fail(f"unknown_command_id: {args.command_id!r}")
    if args.command_id not in APPROVED_LIVE_COMMAND_IDS:
        return _fail(f"command_id_not_approved_for_live_execution: {args.command_id!r}")

    # -- validate repo ----------------------------------------------------------
    if not isinstance(TRUSTED_REPO, str) or args.repo != TRUSTED_REPO:
        return _fail(f"repo_mismatch: {args.repo!r} != {TRUSTED_REPO!r}")

    # -- git remote origin binding ----------------------------------------------
    origin_err = _verify_git_remote_origin(PROJECT_ROOT, TRUSTED_REPO)
    if origin_err:
        return _fail(origin_err)

    # -- find gh binary -----------------------------------------------------------
    gh_bin, gh_err = _find_gh_bin()
    if gh_bin is None:
        return _fail(gh_err)

    # -- AC15: issue binding (mandatory for legacy, optional-but-matching for new)
    env_err = _check_issue_env_binding(args.command_id, args.issue_number)
    if env_err:
        return _fail(env_err)

    if args.input_file is None:
        return _fail("missing_input_source: --input-file is required")

    # -- input-file binding ---------------------------------------------------
    canonical_input, input_err = _validate_and_resolve_input_file(
        args.input_file, args.issue_number, PROJECT_ROOT, command_id=args.command_id
    )
    if input_err:
        return _fail(input_err)

    # -- AC10: per-command-id input schema validation ------------------------
    input_data, json_err = _load_and_validate_input_json(
        canonical_input, args.issue_number, args.command_id
    )
    if json_err:
        return _fail(json_err)

    if args.command_id == COMMAND_ID_ISSUE_BODY_UPDATE:
        return _run_issue_body_update(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_CONTENT_UPDATE:
        return _run_issue_content_update(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_COMMENT_PUBLISH:
        return _run_issue_comment_publish(args, canonical_input, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH:
        return _run_contract_snapshot_publish(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE:
        return _run_issue_scope_snapshot_materialize(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_TEST_VERDICT_PUBLISH:
        return _run_test_verdict_publish(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_DEPENDENCY_REMOVE:
        return _run_issue_dependency_remove(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE:
        return _run_issue_relationship_update(args, input_data, gh_bin, _fail, _ok)

    return _fail(f"unhandled_command_id: {args.command_id!r}")  # pragma: no cover — defensive


def _run_issue_scope_snapshot_materialize(args, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_issue_scope_snapshot_materialize_fields(input_data, args.repo)
    if field_err:
        return _fail(field_err)
    if input_data["worktree_path"] != str(PROJECT_ROOT.resolve()):
        return _fail("issue_scope_snapshot_materialize_worktree_binding_mismatch")
    expected_output = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/issue_scope_snapshot.json"
    if input_data["output_path"] != expected_output:
        return _fail("issue_scope_snapshot_materialize_output_binding_mismatch")
    materializer_path = (PROJECT_ROOT / _ISSUE_SCOPE_SNAPSHOT_MATERIALIZER_REL).resolve()
    if not materializer_path.exists() or not materializer_path.is_relative_to(
        PROJECT_ROOT.resolve()
    ):
        return _fail("issue_scope_snapshot_materializer_module_shadowing")
    if args.dry_run:
        return _ok({"status_detail": "dry_run_ok"})
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )
    # Issue #2163: pre-mutation metadata snapshot captured before the
    # materializer runs, threaded into the postcondition check below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="failed",
        )
    try:
        from materialize_issue_scope_snapshot import materialize

        # Issue #1629 fix_delta P1 (untrusted_gh_git_env): a resolved, trusted
        # gh_bin and a sanitized subprocess env are threaded into the
        # materializer explicitly, the same way every other controlled
        # mutation command id in this executor does -- the materializer must
        # never fall back to an ambient "gh"/"git" on PATH with an
        # unsanitized environment.
        result = materialize(
            issue_number=args.issue_number,
            repo=args.repo,
            contract_snapshot_url=input_data["contract_snapshot_url"],
            base_ref=input_data["base_ref"],
            branch_name=input_data["branch_name"],
            worktree_path=input_data["worktree_path"],
            output=input_data["output_path"],
            gh_bin=gh_bin,
            env=_build_metadata_sanitized_env(),
            project_root=PROJECT_ROOT,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"issue_scope_snapshot_materialize_failed: {exc}", status="failed")
    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
    )
    if changed:
        return _fail("postcondition_tracked_changes_detected", changed, status="failed")
    return _ok({"materializer_result": result})


def _issue_metadata_marker_path(
    project_root: Path, issue_number: int, command_id: str, name: str
) -> Path:
    return (
        project_root
        / "artifacts"
        / str(issue_number)
        / ISSUE_METADATA_NAMESPACE_SEGMENT
        / command_id
        / name
    )


def _run_issue_body_update(args, input_data, gh_bin, _fail, _ok) -> int:
    # -- AC9: per-field schema validation (includes new_body_sha256 self-check)
    field_err = _validate_issue_body_update_fields(input_data)
    if field_err:
        return _fail(field_err)

    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_body_update.marker.json"
    )
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )

    if args.dry_run:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "dry_run_ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # Issue #2163: pre-mutation metadata snapshot captured before any remote
    # PATCH is attempted, threaded into the postcondition check below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="failed",
        )

    # -- Blocker 1: local marker is cache/audit only, never remote-mutation
    # authority. Marker metadata is checked for consistency, but success or
    # failure is decided by a fresh remote readback below.
    marker_data = None
    marker_state = "absent"
    if marker_path.exists():
        try:
            marker_data = json.loads(marker_path.read_text())
        except Exception:  # noqa: BLE001
            return _fail("issue_body_update_marker_corrupt")
        if (
            marker_data.get("issue_number") != args.issue_number
            or marker_data.get("repo") != args.repo
        ):
            return _fail("issue_body_update_marker_metadata_mismatch")

    # -- Readback current remote state (authority for both the marker-hit path
    # and the normal stale-write precondition path).
    body, updated_at, err = _fetch_issue_body_and_updated_at(args.issue_number, args.repo, gh_bin)
    if err:
        return _fail(err, status="failed")
    current_body_sha256 = "sha256:" + hashlib.sha256((body or "").encode("utf-8")).hexdigest()

    # -- Issue #2163 P1-3 (markerless_remote_recovery_missing): the remote
    # readback is checked against the desired state BEFORE the marker-present
    # branch below, and regardless of whether a local marker exists. A prior
    # attempt whose remote PATCH succeeded but whose local marker-write or
    # postcondition check failed afterward must be recoverable on retry: the
    # marker is (re)written here so the transaction becomes idempotent even
    # when no local marker survived the earlier failure.
    if current_body_sha256 == input_data["new_body_sha256"]:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_BODY_UPDATE_MARKER_V1",
                    "issue_number": args.issue_number,
                    "repo": args.repo,
                    "new_body_sha256": current_body_sha256,
                    "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return _ok(
            {
                "status_detail": "already_applied",
                "marker_state": (
                    "already_applied_remote_authority"
                    if marker_data is not None
                    else "already_applied_marker_repaired"
                ),
                "new_body_sha256": current_body_sha256,
                "idempotency_marker_found": marker_data is not None,
                "idempotency_marker_repaired": marker_data is None,
            }
        )

    if marker_data is not None:
        marker_state = "stale_local_marker_recovered"

    # -- AC9: stale-write precondition — readback must match previous_* --------
    if current_body_sha256 != input_data["previous_body_sha256"]:
        return _fail(
            f"stale_precondition_body_sha256_mismatch: current={current_body_sha256} "
            f"expected={input_data['previous_body_sha256']}",
            status="failed",
        )
    if updated_at != input_data["previous_updated_at"]:
        return _fail(
            f"stale_precondition_updated_at_mismatch: current={updated_at} "
            f"expected={input_data['previous_updated_at']}",
            status="failed",
        )

    # -- Mutate ------------------------------------------------------------------
    patch_err = _patch_issue_body(args.issue_number, args.repo, input_data["new_body"], gh_bin)
    if patch_err:
        return _fail(patch_err, status="failed")

    # -- AC4/AC9: postcondition readback — new_body_sha256 must match ------------
    body_after, _updated_at_after, err_after = _fetch_issue_body_and_updated_at(
        args.issue_number, args.repo, gh_bin
    )
    if err_after:
        return _fail(err_after, status="failed")
    actual_new_sha256 = "sha256:" + hashlib.sha256((body_after or "").encode("utf-8")).hexdigest()
    if actual_new_sha256 != input_data["new_body_sha256"]:
        return _fail(
            f"postcondition_new_body_sha256_mismatch: actual={actual_new_sha256} "
            f"expected={input_data['new_body_sha256']}",
            status="failed",
        )

    # -- AC14 / Blocker 6: postcondition -- no changes outside this command's
    # own write root (artifacts/{issue}/issue-metadata/issue_body.update/).
    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
    )
    if changed:
        # Issue #2163 P1-3: the remote PATCH already succeeded (postcondition
        # readback above matched new_body_sha256) -- this failure is a purely
        # LOCAL problem (an unrelated tracked/untracked change was detected),
        # not a mutation failure. Classified distinctly from an ordinary
        # mutation failure ("applied_but_local_postcondition_failed", not
        # "failed") and carries a remote receipt so the caller can decide
        # whether to retry (a retry will hit the already_applied/marker-repair
        # path above rather than re-PATCHing) or reconcile out of band.
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="applied_but_local_postcondition_failed",
            extra={
                "mutation_outcome": "applied",
                "remote_receipt": {
                    "issue_number": args.issue_number,
                    "repo": args.repo,
                    "body_sha256": actual_new_sha256,
                    "observed_updated_at": _updated_at_after,
                },
                "retry_policy": "safe_to_retry_next_attempt_will_detect_already_applied_and_repair_marker",
            },
        )

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "schema": "ISSUE_BODY_UPDATE_MARKER_V1",
                "issue_number": args.issue_number,
                "repo": args.repo,
                "new_body_sha256": actual_new_sha256,
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return _ok(
        {
            "new_body_sha256": actual_new_sha256,
            "idempotency_marker_written": True,
            "marker_state": marker_state,
        }
    )


def _run_issue_content_update(args, input_data, gh_bin, _fail, _ok) -> int:
    """Execute the closed title/body update contract without retrying PATCH."""
    field_err = _validate_issue_content_update_fields(input_data, args.repo, args.issue_number)
    if field_err:
        return _fail(field_err)

    if args.dry_run:
        return _ok({"status_detail": "dry_run_ok", "command_id": args.command_id})

    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_content_update.marker.json"
    )
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )
    # Issue #2163: pre-mutation metadata snapshot captured before any remote
    # PATCH is attempted, threaded into _finalize_remote_success's postcondition
    # check below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="failed",
        )

    before, err = _fetch_issue_content(args.issue_number, args.repo, gh_bin)
    if before is None:
        return _fail(err, status="failed")
    if before.get("isPullRequest"):
        return _fail("target_is_pull_request")
    if not isinstance(before.get("title"), str):
        return _fail("gh_issue_fetch_schema_invalid", status="failed")
    current_body_sha256 = "sha256:" + hashlib.sha256(before["body"].encode("utf-8")).hexdigest()

    def _write_marker_atomically(body_sha256: str) -> str:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker_path.with_name(f".{marker_path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "schema": "ISSUE_CONTENT_UPDATE_MARKER_V1",
                        "issue_number": args.issue_number,
                        "repo": args.repo,
                        "idempotency_key": input_data["idempotency_key"],
                        "new_title": input_data["new_title"],
                        "new_body_sha256": body_sha256,
                        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, marker_path)
            return ""
        except Exception as exc:  # noqa: BLE001
            try:
                temporary.unlink()
            except OSError:
                pass
            return f"issue_content_update_marker_write_failed: {exc}"

    def _finalize_remote_success(
        *, status_detail: str, body_sha256: str, patch_attempted: bool
    ) -> int:
        changed = _check_no_tracked_changes(
            PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
        )
        if changed:
            return _fail(
                "postcondition_tracked_changes_detected",
                status="failed",
                extra={"patch_attempted": patch_attempted, "mutation_outcome": "applied"},
            )
        marker_error = _write_marker_atomically(body_sha256)
        if marker_error:
            return _fail(
                marker_error,
                status="failed",
                extra={"patch_attempted": patch_attempted, "mutation_outcome": "applied"},
            )
        return _ok(
            {
                "status_detail": status_detail,
                "mutation_outcome": "applied" if patch_attempted else status_detail,
                "patch_attempted": patch_attempted,
                "new_title": input_data["new_title"],
                "new_body_sha256": body_sha256,
                "idempotency_marker_written": True,
            }
        )

    # Remote state is authoritative. Local marker state is repairable audit
    # cache only and never decides whether a PATCH is needed.
    if (
        before["title"] == input_data["new_title"]
        and current_body_sha256 == input_data["new_body_sha256"]
    ):
        expected_matches = (
            before["title"] == input_data["expected_previous_title"]
            and current_body_sha256 == input_data["expected_previous_body_sha256"]
            and before["updatedAt"] == input_data["expected_previous_updated_at"]
        )
        return _finalize_remote_success(
            status_detail="no_change" if expected_matches else "already_applied",
            body_sha256=current_body_sha256,
            patch_attempted=False,
        )
    if before["title"] != input_data["expected_previous_title"]:
        return _fail("stale_precondition_title_mismatch", status="failed")
    if current_body_sha256 != input_data["expected_previous_body_sha256"]:
        return _fail("stale_precondition_body_sha256_mismatch", status="failed")
    if before["updatedAt"] != input_data["expected_previous_updated_at"]:
        return _fail("stale_precondition_updated_at_mismatch", status="failed")

    patch_err = _patch_issue_content(
        args.issue_number, args.repo, input_data["new_title"], input_data["new_body"], gh_bin
    )
    if patch_err:
        # PATCH timeout/transport ambiguity is read back once. Never retry PATCH.
        after_ambiguous, after_err = _fetch_issue_content(args.issue_number, args.repo, gh_bin)
        if after_ambiguous is not None:
            body_sha = (
                "sha256:" + hashlib.sha256(after_ambiguous["body"].encode("utf-8")).hexdigest()
            )
            if (
                after_ambiguous["title"] == input_data["new_title"]
                and body_sha == input_data["new_body_sha256"]
            ):
                return _finalize_remote_success(
                    status_detail="already_applied", body_sha256=body_sha, patch_attempted=True
                )
        return _fail(
            patch_err if not after_err else f"{patch_err}; readback={after_err}",
            status="failed",
            extra={"patch_attempted": True, "mutation_outcome": "unknown"},
        )

    after, err = _fetch_issue_content(args.issue_number, args.repo, gh_bin)
    if after is None:
        return _fail(
            err, status="failed", extra={"patch_attempted": True, "mutation_outcome": "unknown"}
        )
    if after.get("isPullRequest"):
        return _fail(
            "target_is_pull_request",
            status="failed",
            extra={"patch_attempted": True, "mutation_outcome": "unknown"},
        )
    if not isinstance(after.get("title"), str):
        return _fail(
            "gh_issue_fetch_schema_invalid",
            status="failed",
            extra={"patch_attempted": True, "mutation_outcome": "unknown"},
        )
    actual_new_body_sha256 = "sha256:" + hashlib.sha256(after["body"].encode("utf-8")).hexdigest()
    if after["title"] != input_data["new_title"]:
        return _fail(
            "postcondition_new_title_mismatch",
            status="failed",
            extra={"patch_attempted": True, "mutation_outcome": "unknown"},
        )
    if actual_new_body_sha256 != input_data["new_body_sha256"]:
        return _fail(
            "postcondition_new_body_sha256_mismatch",
            status="failed",
            extra={"patch_attempted": True, "mutation_outcome": "unknown"},
        )
    return _finalize_remote_success(
        status_detail="applied", body_sha256=actual_new_body_sha256, patch_attempted=True
    )


def _run_issue_comment_publish(args, canonical_input, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_issue_comment_publish_fields(input_data)
    if field_err:
        return _fail(field_err)

    if _is_human_history_body_or_marker(input_data["comment_body"], input_data["marker"]):
        return _run_human_history_comment_publish(args, input_data, gh_bin, _fail, _ok)

    marker = input_data["marker"]
    comment_body = input_data["comment_body"]
    expected_body_sha256 = hashlib.sha256(comment_body.encode()).hexdigest()
    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_comment_publish.marker.json"
    )
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )

    if args.dry_run:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "dry_run_ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # Issue #2163: pre-mutation metadata snapshot captured before any remote
    # POST is attempted, threaded into the postcondition check below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="failed",
        )

    def _write_marker(comment_id, comment_url) -> None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_COMMENT_PUBLISH_MARKER_V1",
                    "issue_number": args.issue_number,
                    "repo": args.repo,
                    "marker": marker,
                    "comment_id": comment_id,
                    "comment_url": comment_url,
                    "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    # -- Blocker 2/3: pre-mutation remote marker precheck. A local marker file
    # is never authority by itself; remote GitHub state decides no-op vs. post
    # vs. conflict, and this check runs BEFORE any POST so a failed transaction
    # never leaves a remote side effect.
    matches, list_err = _find_marker_matches(marker, args.issue_number, args.repo, gh_bin)
    if list_err:
        return _fail(f"marker_precheck_failed: {list_err}", status="failed")

    if len(matches) > 1:
        return _fail("duplicate_marker_conflict_pre_mutation", status="failed")

    if len(matches) == 1:
        c = matches[0]
        remote_body_sha256 = hashlib.sha256(c.get("body", "").encode()).hexdigest()
        if remote_body_sha256 == expected_body_sha256:
            # No-op: already published by a prior run (or another agent).
            # Refresh the local cache/audit marker but do not POST again.
            _write_marker(c.get("id", ""), c.get("url", ""))
            return _ok(
                {
                    "status_detail": "already_published",
                    "comment_id": c.get("id", ""),
                    "comment_url": c.get("url", ""),
                    "body_sha256": remote_body_sha256,
                    "idempotency_marker_written": True,
                }
            )

        # -- Issue #1116 AC4: ownership marker matches but content/digest
        # differs. This is no longer an unconditional conflict -- a caller
        # (e.g. finalize_no_diff_issue.py, which separates a stable
        # ownership marker from a content digest marker) may legitimately
        # need to update its own previously-published marker comment when
        # the payload changes. PATCH is only attempted when the existing
        # comment's author is the currently authenticated actor (mirrors the
        # `_validate_test_verdict_comment` author-check pattern); any other
        # author is a genuine identity conflict and must fail closed without
        # ever calling PATCH.
        authenticated_login, login_err = _fetch_authenticated_login(gh_bin)
        if login_err:
            return _fail(f"marker_update_author_check_failed: {login_err}", status="failed")
        existing_author = (c.get("author") or {}).get("login")
        if existing_author != authenticated_login:
            return _fail("remote_marker_author_mismatch_pre_mutation", status="failed")

        numeric_comment_id = _extract_numeric_comment_id_from_url(c.get("url", ""))
        if not numeric_comment_id:
            return _fail("remote_marker_comment_id_unresolvable_pre_mutation", status="failed")

        patch_err = _patch_gh_comment(numeric_comment_id, args.repo, comment_body, gh_bin)
        if patch_err:
            return _fail(
                patch_err,
                status="failed",
                extra={"patch_attempted": True, "mutation_outcome": "unknown"},
            )

        readback = _readback_by_marker_literal(marker, args.issue_number, args.repo, gh_bin)
        if "error" in readback:
            return _fail(
                f"readback_failed: {readback['error']}",
                status="failed",
                extra={"patch_attempted": True, "mutation_outcome": "unknown"},
            )
        if readback.get("body_sha256") != expected_body_sha256:
            return _fail(
                "postcondition_body_sha256_mismatch",
                status="failed",
                extra={"patch_attempted": True, "mutation_outcome": "unknown"},
            )

        changed = _check_no_tracked_changes(
            PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
        )
        if changed:
            return _fail(
                "postcondition_tracked_changes_detected",
                [f"changed: {f}" for f in changed[:20]],
                status="applied_but_local_postcondition_failed",
                extra={
                    "mutation_outcome": "applied",
                    "remote_receipt": {
                        "issue_number": args.issue_number,
                        "repo": args.repo,
                        "comment_id": readback.get("comment_id"),
                        "comment_url": readback.get("comment_url"),
                        "body_sha256": readback.get("body_sha256"),
                    },
                    "retry_policy": "safe_to_retry_remote_marker_precheck_will_detect_already_published",
                },
            )

        _write_marker(readback.get("comment_id"), readback.get("comment_url"))
        return _ok(
            {
                "status_detail": "updated",
                "comment_id": readback.get("comment_id"),
                "comment_url": readback.get("comment_url"),
                "body_sha256": readback.get("body_sha256"),
                "idempotency_marker_written": True,
            }
        )

    # -- matches == 0: no remote marker yet, proceed to post --------------------
    comment_url, comment_id, post_err = _post_gh_comment(  # noqa: RUF059
        args.issue_number, args.repo, comment_body, gh_bin
    )
    if post_err:
        # Issue #1116 fix_delta P1-2: a POST response can be lost after the
        # remote mutation actually succeeded (same transport-ambiguity class
        # already handled by the PATCH/issue_content.update branches above).
        # Read back once by marker literal before declaring failure -- never
        # retry the POST itself within the same invocation.
        post_readback = _readback_by_marker_literal(marker, args.issue_number, args.repo, gh_bin)
        if post_readback.get("error") == "marker_not_found":
            # Confirmed: nothing was created remotely by this POST.
            return _fail(post_err, status="failed")
        if "error" in post_readback or post_readback.get("body_sha256") != expected_body_sha256:
            # Readback itself is inconclusive, or a comment matching the
            # marker exists with unexpected content -- POST success/failure
            # cannot be determined either way. Fail closed as ambiguous.
            return _fail(
                post_err,
                status="failed",
                extra={"patch_attempted": False, "mutation_outcome": "unknown"},
            )

        changed = _check_no_tracked_changes(
            PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
        )
        if changed:
            return _fail(
                "postcondition_tracked_changes_detected",
                [f"changed: {f}" for f in changed[:20]],
                status="applied_but_local_postcondition_failed",
                extra={
                    "mutation_outcome": "applied",
                    "remote_receipt": {
                        "issue_number": args.issue_number,
                        "repo": args.repo,
                        "comment_id": post_readback.get("comment_id"),
                        "comment_url": post_readback.get("comment_url"),
                        "body_sha256": post_readback.get("body_sha256"),
                    },
                    "retry_policy": "safe_to_retry_remote_marker_precheck_will_detect_already_published",
                },
            )

        _write_marker(post_readback.get("comment_id"), post_readback.get("comment_url"))
        return _ok(
            {
                "status_detail": "created",
                "mutation_outcome": "applied",
                "comment_id": post_readback.get("comment_id"),
                "comment_url": post_readback.get("comment_url"),
                "body_sha256": post_readback.get("body_sha256"),
                "idempotency_marker_written": True,
            }
        )

    # -- AC4/AC14: postcondition readback by marker — false success not allowed -
    readback = _readback_by_marker_literal(marker, args.issue_number, args.repo, gh_bin)
    if "error" in readback:
        return _fail(f"readback_failed: {readback['error']}", status="failed")
    if readback.get("body_sha256") != expected_body_sha256:
        return _fail("postcondition_body_sha256_mismatch", status="failed")

    # -- AC14 / Blocker 6: postcondition -- no changes outside this command's
    # own write root (artifacts/{issue}/issue-metadata/issue_comment.publish/).
    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
    )
    if changed:
        # Issue #2163 P1-3: the remote POST already succeeded (marker-literal
        # readback above matched expected_body_sha256) -- this is a purely
        # LOCAL failure, not a mutation failure. A retry does not risk a
        # duplicate POST: the pre-mutation remote marker precheck above
        # (`_find_marker_matches`) will find the just-posted comment by its
        # marker text and take the `already_published` no-op path instead of
        # posting again, so markerless recovery on retry is already safe even
        # though the local marker file was never written on this attempt.
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="applied_but_local_postcondition_failed",
            extra={
                "mutation_outcome": "applied",
                "remote_receipt": {
                    "issue_number": args.issue_number,
                    "repo": args.repo,
                    "comment_id": readback.get("comment_id"),
                    "comment_url": readback.get("comment_url"),
                    "body_sha256": readback.get("body_sha256"),
                },
                "retry_policy": "safe_to_retry_remote_marker_precheck_will_detect_already_published",
            },
        )

    _write_marker(readback.get("comment_id"), readback.get("comment_url"))

    return _ok(
        {
            "status_detail": "created",
            "comment_id": readback.get("comment_id"),
            "comment_url": readback.get("comment_url"),
            "body_sha256": readback.get("body_sha256"),
            "idempotency_marker_written": True,
        }
    )


def _list_issue_comments(issue_number: int, repo: str, gh_bin: str) -> tuple[list[dict], str]:
    """Return raw comment records for the human-history controlled sublane."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "comments"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=_build_metadata_sanitized_env(),
        )
        if out.returncode != 0:
            return [], f"gh_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        if not isinstance(comments, list) or not all(isinstance(item, dict) for item in comments):
            return [], "gh_comments_schema_invalid"
        return comments, ""
    except Exception as exc:  # noqa: BLE001
        return [], f"human_history_comment_list_exception:{exc}"


def _human_history_readback(
    *,
    marker: str,
    expected_digest: str,
    expected_author: str,
    issue_number: int,
    repo: str,
    gh_bin: str,
) -> tuple[dict | None, str]:
    """Read back exactly one owned identity marker and its canonical digest."""
    comments, err = _list_issue_comments(issue_number, repo, gh_bin)
    if err:
        return None, err
    owned: list[tuple[dict, dict]] = []
    for comment in comments:
        body = comment.get("body", "")
        if not isinstance(body, str) or _HUMAN_HISTORY_NAMESPACE not in body:
            continue
        parsed, marker_err = _parse_human_history_marker_source(body)
        if marker_err:
            return None, f"human_history_marker_diagnostic_failure:{marker_err}"
        assert parsed is not None
        if parsed["marker"] != marker:
            continue
        author = (comment.get("author") or {}).get("login")
        if author != expected_author:
            return None, "human_history_remote_marker_author_mismatch"
        owned.append((comment, parsed))
    if len(owned) != 1:
        return None, "human_history_owned_marker_cardinality_invalid"
    comment, parsed = owned[0]
    if parsed["content_digest"] != expected_digest:
        return None, "human_history_readback_content_digest_mismatch"
    return {
        "comment_id": comment.get("id", ""),
        "comment_url": comment.get("url", ""),
        "content_digest": parsed["content_digest"],
        "identity_sha256": parsed["identity_sha256"],
    }, ""


_HUMAN_HISTORY_POST_PR_DECISION_RE = _re.compile(
    r"(?m)^- phase: (?P<phase>post-PR-binding|post-PR-head-drift)\r?$\n"
    r"^- reviewed_ref: refs/pull/(?P<pr_number>[1-9][0-9]*)/head@(?P<reviewed_head>[0-9a-f]{40})\r?$"
)
_HUMAN_HISTORY_STALE_EVIDENCE_HEAD_RE = _re.compile(
    r"(?mi)^- stale evidence: .*?(?P<head>[0-9a-f]{40})(?:\r?$|[^0-9a-f])"
)

# Issue #1908 fix_delta HIGH-1: every valid human-history identity has a
# rendered `- phase: <literal>` line drawn from this fixed six-value set
# (mirrors the publisher's _validate_human_history_identity() matrix
# literals). Enforcing this here -- on the controlled-lane side, from the
# rendered body itself -- closes the gap where a template that lost,
# reordered, or corrupted its phase/reviewed_ref lines would otherwise
# silently look like "no post-PR binding required" and skip the decision-time
# PR-head recheck entirely.
_HUMAN_HISTORY_KNOWN_PHASES = frozenset(
    {
        "review-complete",
        "pre-PR-binding",
        "binding-validation",
        "post-PR-binding",
        "post-PR-head-drift",
        "conflict-resolution",
    }
)
_HUMAN_HISTORY_POST_PR_PHASES = frozenset({"post-PR-binding", "post-PR-head-drift"})
_HUMAN_HISTORY_PHASE_LINE_RE = _re.compile(r"(?m)^- phase: (?P<phase>[^\r\n]*)\r?$")


def _human_history_post_pr_decision_binding(body: str) -> tuple[dict | None, str]:
    """Extract the rendered post-PR state that the controlled lane must bind.

    The comment identity remains the existing seven-field marker digest.  This
    parser reads only the publisher's rendered, public-safe fields so that a
    decision cannot use a caller's cached PR head.  A diagnostic additionally
    carries the latest observed head in its existing ``stale evidence`` text;
    its marker identity deliberately remains bound to the original snapshot.

    A missing/ambiguous/unknown `phase` line, or a post-PR phase whose
    `reviewed_ref` cannot be extracted uniquely in the canonical
    `refs/pull/<pr>/head@<40-lowercase-sha>` form, is a template-corruption
    diagnostic -- not "no binding required". The caller must treat a
    non-empty error here as fail-closed before any create/PATCH/noop
    decision (Issue #1908 fix_delta HIGH-1).
    """
    phase_matches = _HUMAN_HISTORY_PHASE_LINE_RE.findall(body)
    if len(phase_matches) != 1:
        return None, "human_history_phase_field_missing_or_ambiguous"
    phase = phase_matches[0]
    if phase not in _HUMAN_HISTORY_KNOWN_PHASES:
        return None, "human_history_phase_field_unknown"
    if phase not in _HUMAN_HISTORY_POST_PR_PHASES:
        return None, ""

    match = _HUMAN_HISTORY_POST_PR_DECISION_RE.search(body)
    if match is None or match.group("phase") != phase:
        return None, "human_history_post_pr_reviewed_ref_binding_invalid"
    expected_head = match.group("reviewed_head")
    if phase == "post-PR-head-drift":
        stale = _HUMAN_HISTORY_STALE_EVIDENCE_HEAD_RE.search(body)
        if stale is None:
            return None, "human_history_head_drift_stale_evidence_head_missing"
        expected_head = stale.group("head")
    return {
        "phase": phase,
        "pr_number": int(match.group("pr_number")),
        "reviewed_head": match.group("reviewed_head"),
        "decision_head": expected_head,
    }, ""


def _recheck_human_history_pr_head_before_decision(
    *, body: str, repo: str, gh_bin: str
) -> tuple[dict | None, str]:
    """Directly read the PR head immediately before a post-PR decision.

    Returning a structured binding permits the caller to emit a deterministic
    reconciliation route instead of treating a stale create/PATCH/noop as a
    successful publication.  This is intentionally in the controlled
    executor, where the actual remote mutation decision occurs, rather than
    relying on an earlier documentation-level or publisher-level read.
    """
    binding, binding_err = _human_history_post_pr_decision_binding(body)
    if binding_err or binding is None:
        return binding, binding_err
    current_head, head_err = _fetch_pr_head_sha(
        binding["pr_number"], repo, gh_bin, env=_build_metadata_sanitized_env()
    )
    if head_err:
        return None, f"human_history_pr_head_direct_read_failed:{head_err}"
    assert current_head is not None
    binding["current_head"] = current_head
    if current_head != binding["decision_head"]:
        if binding["phase"] == "post-PR-binding":
            return binding, "human_history_primary_head_drift_reconciliation_required"
        return binding, "human_history_diagnostic_head_drift_reconciliation_required"
    return binding, ""


def _human_history_head_drift_result_extra(binding: dict, *, mutation_applied: bool) -> dict:
    """Return the required reconciliation/re-review route without new state.

    ``patch_attempted`` cannot represent a successful create: POST is a
    mutation too, while noop is not.  Report the confirmed write fact rather
    than the PATCH-specific transport detail so post-create head drift never
    claims the already-read-back comment was not applied.
    """
    is_primary = binding["phase"] == "post-PR-binding"
    return {
        "mutation_outcome": "applied" if mutation_applied else "not_applied",
        "head_drift": {
            "phase": binding["phase"],
            "reviewed_head_sha": binding["reviewed_head"],
            "decision_head_sha": binding["decision_head"],
            "current_head_sha": binding["current_head"],
            "route": (
                "reconcile_head_drift_then_rereview"
                if is_primary
                else "reconcile_same_head_drift_identity_then_rereview"
            ),
        },
        "rerun_required": {"pr_review": True},
    }


def _run_human_history_comment_publish(args, input_data, gh_bin, _fail, _ok) -> int:
    """Create, PATCH, or noop one human-history identity in the existing lane.

    Unlike generic comment markers, namespace grammar is security-relevant:
    every raw namespace candidate is diagnosed before a mutation, digesting,
    or author ownership decision.  This leaves existing machine-comment
    discovery and payload semantics unchanged.
    """
    comment_body = input_data["comment_body"]
    marker = input_data["marker"]
    expected, expected_err = _parse_human_history_marker_source(comment_body)
    if expected_err:
        return _fail(f"human_history_marker_diagnostic_failure:{expected_err}")
    assert expected is not None
    if marker != expected["marker"]:
        return _fail("human_history_marker_input_mismatch")
    if args.dry_run:
        return _ok(
            {
                "status_detail": "dry_run_ok",
                "human_history_identity_sha256": expected["identity_sha256"],
                "human_history_content_digest": expected["content_digest"],
            }
        )

    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )
    pre_mutation_snapshot, snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if snapshot_err is not None:
        return _fail(f"pre_mutation_snapshot_capture_failed: {snapshot_err}", status="failed")
    authenticated_login, login_err = _fetch_authenticated_login(gh_bin)
    if login_err:
        return _fail(f"human_history_author_check_failed:{login_err}", status="failed")

    comments, list_err = _list_issue_comments(args.issue_number, args.repo, gh_bin)
    if list_err:
        return _fail(f"human_history_marker_precheck_failed:{list_err}", status="failed")
    matching: list[tuple[dict, dict]] = []
    # Validate every namespace occurrence before deciding whether this identity
    # exists; malformed tokens are diagnostics, never ignorable prose.
    for comment in comments:
        body = comment.get("body", "")
        if not isinstance(body, str) or _HUMAN_HISTORY_NAMESPACE not in body:
            continue
        parsed, parse_err = _parse_human_history_marker_source(body)
        if parse_err:
            return _fail(f"human_history_marker_diagnostic_failure:{parse_err}", status="failed")
        assert parsed is not None
        # All valid human-history markers on this target are controlled-lane
        # state, not merely candidates for this invocation's identity. A
        # foreign or unowned marker therefore makes the target diagnostically
        # unsafe before any create, noop, or PATCH decision. In particular,
        # do not ignore a different-identity foreign marker and create beside
        # it: that would turn a discovery failure into a remote mutation.
        if (comment.get("author") or {}).get("login") != authenticated_login:
            return _fail("human_history_remote_marker_author_mismatch", status="failed")
        if parsed["marker"] == marker:
            matching.append((comment, parsed))
    if len(matching) > 1:
        return _fail("human_history_duplicate_owned_marker", status="failed")

    def fresh_head_before_decision() -> tuple[dict | None, int | None]:
        """Make the decision-time read the last operation before create/PATCH/noop."""
        binding, head_err = _recheck_human_history_pr_head_before_decision(
            body=comment_body, repo=args.repo, gh_bin=gh_bin
        )
        if not head_err:
            return binding, None
        if binding is not None and "reconciliation_required" in head_err:
            return None, _fail(
                head_err,
                status="stale_head",
                extra=_human_history_head_drift_result_extra(binding, mutation_applied=False),
            )
        return None, _fail(head_err, status="failed")

    def finalize(
        status_detail: str,
        *,
        patch_attempted: bool,
        mutation_applied: bool,
        decision_binding: dict | None,
    ) -> int:
        readback, readback_err = _human_history_readback(
            marker=marker,
            expected_digest=expected["content_digest"],
            expected_author=authenticated_login,
            issue_number=args.issue_number,
            repo=args.repo,
            gh_bin=gh_bin,
        )
        if readback_err:
            return _fail(f"human_history_readback_failed:{readback_err}", status="failed")

        # A readback (including an accepted noop) is not a terminal acceptance
        # boundary. Re-read directly before returning so an old primary cannot
        # authorize a new head and a diagnostic must be reconciled again.
        if decision_binding is not None:
            post_binding, post_head_err = _recheck_human_history_pr_head_before_decision(
                body=comment_body, repo=args.repo, gh_bin=gh_bin
            )
            if post_head_err:
                if post_binding is not None and "reconciliation_required" in post_head_err:
                    return _fail(
                        post_head_err,
                        status="applied_but_head_drift" if mutation_applied else "stale_head",
                        extra=_human_history_head_drift_result_extra(
                            post_binding, mutation_applied=mutation_applied
                        ),
                    )
                return _fail(post_head_err, status="failed")

        changed = _check_no_tracked_changes(
            PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
        )
        if changed:
            return _fail(
                "postcondition_tracked_changes_detected",
                [f"changed: {item}" for item in changed[:20]],
                status="applied_but_local_postcondition_failed",
            )
        assert readback is not None
        return _ok(
            {
                "status_detail": status_detail,
                "comment_id": readback["comment_id"],
                "comment_url": readback["comment_url"],
                "human_history_identity_sha256": readback["identity_sha256"],
                "human_history_content_digest": readback["content_digest"],
                "patch_attempted": patch_attempted,
            }
        )

    if matching:
        comment, remote = matching[0]
        if remote["content_digest"] == expected["content_digest"]:
            # A noop is still read back through the same marker/author/digest
            # boundary so it cannot silently accept a stale or foreign state.
            decision_binding, decision_result = fresh_head_before_decision()
            if decision_result is not None:
                return decision_result
            return finalize(
                "already_published",
                patch_attempted=False,
                mutation_applied=False,
                decision_binding=decision_binding,
            )
        numeric_comment_id = _extract_numeric_comment_id_from_url(comment.get("url", ""))
        if not numeric_comment_id:
            return _fail("human_history_remote_marker_comment_id_unresolvable", status="failed")
        decision_binding, decision_result = fresh_head_before_decision()
        if decision_result is not None:
            return decision_result
        patch_err = _patch_gh_comment(numeric_comment_id, args.repo, comment_body, gh_bin)
        if patch_err:
            return _fail(
                patch_err,
                status="failed",
                extra={"patch_attempted": True, "mutation_outcome": "unknown"},
            )
        return finalize(
            "updated",
            patch_attempted=True,
            mutation_applied=True,
            decision_binding=decision_binding,
        )

    decision_binding, decision_result = fresh_head_before_decision()
    if decision_result is not None:
        return decision_result
    _url, _id, post_err = _post_gh_comment(args.issue_number, args.repo, comment_body, gh_bin)
    if post_err:
        # POST may have succeeded while its response was lost.  A single strict
        # readback reconciles that ambiguity without a second POST.
        readback, readback_err = _human_history_readback(  # noqa: RUF059
            marker=marker,
            expected_digest=expected["content_digest"],
            expected_author=authenticated_login,
            issue_number=args.issue_number,
            repo=args.repo,
            gh_bin=gh_bin,
        )
        if readback_err:
            return _fail(post_err, status="failed", extra={"mutation_outcome": "unknown"})
        return finalize(
            "created_reconciled",
            patch_attempted=False,
            mutation_applied=True,
            decision_binding=decision_binding,
        )
    return finalize(
        "created", patch_attempted=False, mutation_applied=True, decision_binding=decision_binding
    )


def _find_marker_matches(
    marker_literal: str, issue_number: int, repo: str, gh_bin: str
) -> tuple[list[dict], str]:
    """List all remote comments containing marker_literal. Returns (matches, error).

    Used as the pre-mutation precheck for issue_comment.publish (Blocker 3):
    the caller must know remote marker count/identity BEFORE deciding whether
    to POST, so that a failed transaction never leaves a remote side effect.
    """
    try:
        out = subprocess.run(  # noqa: PLW1510
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "comments"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if out.returncode != 0:
            return [], f"gh_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        matches = [c for c in comments if marker_literal in c.get("body", "")]
        return matches, ""
    except Exception as exc:  # noqa: BLE001
        return [], f"marker_list_exception:{exc}"


def _readback_by_marker_literal(
    marker_literal: str, issue_number: int, repo: str, gh_bin: str
) -> dict:
    """Search comments for a literal marker string (issue_comment.publish uses
    caller-provided markers, not an executor-generated wrapper marker)."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "comments"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if out.returncode != 0:
            return {"error": f"gh_failed_rc_{out.returncode}"}
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        matches = [c for c in comments if marker_literal in c.get("body", "")]
        if len(matches) == 0:
            return {"error": "marker_not_found"}
        if len(matches) > 1:
            return {"error": f"marker_found_{len(matches)}_times"}
        c = matches[0]
        body = c.get("body", "")
        return {
            "comment_id": c.get("id", ""),
            "comment_url": c.get("url", ""),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"readback_exception:{exc}"}


# Issue #1116 AC4: single-caller helpers for `_run_issue_comment_publish`'s
# marker-match + digest-mismatch update branch. `_find_marker_matches` /
# `_readback_by_marker_literal` return the GraphQL node id (via
# `gh issue view --json comments`), which is not a valid REST path segment,
# so the REST-compatible numeric comment id is instead extracted from the
# comment's permalink (`.../issues/<n>#issuecomment-<numeric-id>`).
_ISSUE_COMMENT_URL_ID_RE = _re.compile(r"issuecomment-(\d+)$")


def _extract_numeric_comment_id_from_url(comment_url: str) -> str:
    """Extract the numeric REST comment id from a GitHub issue comment
    permalink. Returns "" if the url does not match the expected shape.
    Single caller: `_run_issue_comment_publish`."""
    match = _ISSUE_COMMENT_URL_ID_RE.search(comment_url or "")
    return match.group(1) if match else ""


def _patch_gh_comment(numeric_comment_id: str, repo: str, body: str, gh_bin: str) -> str:
    """PATCH an existing issue comment's body via the REST API (Issue #1116
    AC4 update branch). Mirrors `_post_gh_comment`'s argv-list / tempfile
    pattern. Single caller: `_run_issue_comment_publish`."""
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        try:
            out = subprocess.run(  # noqa: PLW1510
                [
                    gh_bin,
                    "api",
                    "--hostname",
                    _TRUSTED_GITHUB_HOST,
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/comments/{numeric_comment_id}",
                    "--field",
                    f"body=@{tmp_path}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
                env=_build_metadata_sanitized_env(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out.returncode != 0:
            return _classify_gh_error("gh_api_comment_patch_failed", out.stderr or "")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"gh_api_comment_patch_exception: {exc}"


# -- Shared `gh` helpers originally introduced for the controlled PR review
# -- publisher (pr_review.publish, removed in Issue #1873); retained because
# -- test_verdict.publish still depends on them for its own PR-head/identity
# -- readback and marker-position checks.

# Issue #1539 fix_delta Blocker 2: env vars that must never reach the `gh`
# subprocess, beyond the generic ENV_SANITIZE_KEYS (GH_HOST / GH_REPO /
# GH_CONFIG_DIR / GH_DEBUG / DEBUG can silently redirect `gh` to a different
# host/config or leak debug output; an inherited parent env is never trusted
# here).
_PR_REVIEW_GH_ENV_STRIP_KEYS = frozenset(ENV_SANITIZE_KEYS) | frozenset(
    {
        "GH_HOST",
        "GH_REPO",
        "GH_CONFIG_DIR",
        "GH_DEBUG",
        "DEBUG",
    }
)


def _build_pr_review_gh_env() -> dict[str, str]:
    """Sanitized environment for every `gh` subprocess call made while
    publishing a PR review or test verdict. Built fresh (not memoized) so
    each call gets an independent copy that later mutation cannot
    cross-contaminate."""
    env = os.environ.copy()
    for key in _PR_REVIEW_GH_ENV_STRIP_KEYS:
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _marker_at_expected_position(body: str, marker_str: str) -> bool:
    """True iff marker_str occurs exactly once in body AND that occurrence is
    the trailing content (i.e. the publisher's own appended marker, not an
    unrelated mid-body substring match -- Issue #1539 fix_delta Blocker 3)."""
    if not body or body.count(marker_str) != 1:
        return False
    return body.rstrip("\n").endswith(marker_str)


def _fetch_pr_head_sha(
    pr_number: int, repo: str, gh_bin: str, env: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """Fetch the current remote PR head commit SHA. Returns (sha, error)."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/pulls/{pr_number}",
                "--jq",
                ".head.sha",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_pr_head_fetch_failed", out.stderr or "")
        sha = out.stdout.strip()
        if not _PR_HEAD_SHA_RE.match(sha):
            return None, f"gh_api_pr_head_unexpected_output: {sha!r}"
        return sha, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_api_pr_head_fetch_exception: {exc}"


def _fetch_authenticated_login(
    gh_bin: str, env: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """Fetch the authenticated gh identity's login. Used as a postcondition
    identity binding when re-verifying an idempotent-retry review (Issue #1539
    fix_delta Blocker 3): the review author must be the SAME identity this
    process is authenticated as, not an unrelated/spoofed account."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST, "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_authenticated_user_failed", out.stderr or "")
        login = out.stdout.strip()
        if not login:
            return None, "gh_api_authenticated_user_empty"
        return login, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_api_authenticated_user_exception: {exc}"


# -- Issue #1647: dedicated receipt-bound TEST_VERDICT publisher ------------


def _fetch_linked_issue_body_sha256(
    issue_number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[str | None, str]:
    # Issue #1647 Scope Delta AC11: the linked Issue endpoint must not
    # resolve to a pull request. GitHub's Issues REST endpoint returns PRs
    # too; the presence of the `pull_request` key is the documented signal
    # (mirrors _fetch_issue_content's `isPullRequest: has("pull_request")`
    # jq expression).
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/{issue_number}",
                "--jq",
                '{body, isPullRequest: has("pull_request")}',
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_linked_issue_fetch_failed", out.stderr or "")
        parsed = json.loads(out.stdout)
        if not isinstance(parsed, dict):
            return None, "gh_api_linked_issue_body_invalid"
        if parsed.get("isPullRequest"):
            return None, "test_verdict_linked_issue_is_pull_request"
        body = parsed.get("body")
        if not isinstance(body, str):
            return None, "gh_api_linked_issue_body_invalid"
        return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_api_linked_issue_fetch_exception: {exc}"


def _find_test_verdict_marker_matches(
    marker: str, pr_number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[list[dict], str]:
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/{pr_number}/comments",
                "--paginate",
                "--jq",
                ".",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return [], _classify_gh_error(
                "gh_api_test_verdict_comment_list_failed", out.stderr or ""
            )
        comments: list[dict] = []
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            comments.extend(parsed if isinstance(parsed, list) else [parsed])
        return [
            c for c in comments if _marker_at_expected_position(c.get("body") or "", marker)
        ], ""
    except Exception as exc:  # noqa: BLE001
        return [], f"test_verdict_marker_list_exception: {exc}"


def _post_test_verdict_comment(
    pr_number: int, repo: str, body: str, gh_bin: str, env: dict[str, str]
) -> tuple[dict | None, str]:
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                "--method",
                "POST",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--input",
                "-",
            ],
            input=json.dumps({"body": body}),
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error(
                "gh_api_test_verdict_comment_post_failed", out.stderr or ""
            )
        return json.loads(out.stdout), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"test_verdict_comment_post_exception: {exc}"


def _readback_test_verdict_comment(
    comment_id: object, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[dict | None, str]:
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/comments/{comment_id}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error(
                "gh_api_test_verdict_comment_readback_failed", out.stderr or ""
            )
        return json.loads(out.stdout), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"test_verdict_comment_readback_exception: {exc}"


def _validate_test_verdict_comment(
    comment: dict, marker: str, body_sha256: str, authenticated_login: str
) -> str:
    if not _marker_at_expected_position(comment.get("body") or "", marker):
        return "test_verdict_postcondition_marker_mismatch"
    body = comment["body"]
    marker_idx = body.rfind(marker)
    raw_body = body[:marker_idx]
    raw_body = raw_body.removesuffix("\n\n")
    if hashlib.sha256(raw_body.encode("utf-8")).hexdigest() != body_sha256:
        return "test_verdict_postcondition_body_mismatch"
    if (comment.get("user") or {}).get("login") != authenticated_login:
        return "test_verdict_postcondition_author_mismatch"
    if not comment.get("id") or not comment.get("html_url"):
        return "test_verdict_postcondition_comment_identity_missing"
    return ""


def _gh_api_get(gh_bin: str, env: dict, path: str) -> tuple[dict | None, str]:
    """Generic single-endpoint JSON GET used by the Issue #1647 Scope Delta
    live readback checks (AC6). Never uses shell redirection."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST, path],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_fetch_failed", out.stderr or "")
        return json.loads(out.stdout), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_api_fetch_exception: {exc}"


def _verify_producer_run_and_job(
    receipt: dict, repo: str, expected_head_sha: str, gh_bin: str, env: dict
) -> str:
    """Issue #1647 Scope Delta AC6: live readback of the GitHub Actions
    workflow run / job / check run the receipt claims to originate from.
    Confirms the run's repository and head SHA, that the job belongs to
    that run, and that the check run is linked to the same job/run."""
    producer = receipt.get("producer") or {}
    run_id = producer.get("workflow_run_id")
    run_attempt = producer.get("workflow_run_attempt")
    job_id = producer.get("job_id")
    check_run_id = producer.get("check_run_id")

    run_data, err = _gh_api_get(
        gh_bin, env, f"repos/{repo}/actions/runs/{run_id}/attempts/{run_attempt}"
    )
    if err:
        return f"gh_api_workflow_run_fetch_failed: {err}"
    if not isinstance(run_data, dict) or run_data.get("head_sha") != expected_head_sha:
        return "test_verdict_publish_receipt_workflow_run_head_sha_mismatch"
    if (run_data.get("repository") or {}).get("full_name") != repo:
        return "test_verdict_publish_receipt_workflow_run_repository_mismatch"

    job_data, err = _gh_api_get(gh_bin, env, f"repos/{repo}/actions/jobs/{job_id}")
    if err:
        return f"gh_api_job_fetch_failed: {err}"
    if not isinstance(job_data, dict) or job_data.get("run_id") != run_id:
        return "test_verdict_publish_receipt_job_run_id_mismatch"
    if job_data.get("head_sha") != expected_head_sha:
        return "test_verdict_publish_receipt_job_head_sha_mismatch"
    check_run_url = job_data.get("check_run_url") or ""
    if not check_run_url.endswith(f"/check-runs/{check_run_id}"):
        return "test_verdict_publish_receipt_job_check_run_linkage_mismatch"

    check_data, err = _gh_api_get(gh_bin, env, f"repos/{repo}/check-runs/{check_run_id}")
    if err:
        return f"gh_api_check_run_fetch_failed: {err}"
    if not isinstance(check_data, dict) or check_data.get("id") != check_run_id:
        return "test_verdict_publish_receipt_check_run_id_mismatch"
    if check_data.get("head_sha") != expected_head_sha:
        return "test_verdict_publish_receipt_check_run_head_sha_mismatch"
    return ""


def _verify_execution_artifact_metadata(receipt: dict, repo: str, gh_bin: str, env: dict) -> str:
    """Issue #1647 Scope Delta AC6: live readback of the execution-record
    artifact metadata (id / non-expired) the receipt claims."""
    artifact = receipt.get("execution_artifact") or {}
    artifact_id = artifact.get("artifact_id")
    meta, err = _gh_api_get(gh_bin, env, f"repos/{repo}/actions/artifacts/{artifact_id}")
    if err:
        return f"gh_api_artifact_metadata_fetch_failed: {err}"
    if not isinstance(meta, dict) or meta.get("id") != artifact_id:
        return "test_verdict_publish_receipt_artifact_metadata_id_mismatch"
    if meta.get("expired") is not False:
        return "test_verdict_publish_receipt_artifact_metadata_expired"
    return ""


def _download_and_verify_artifact_archive(
    receipt: dict, repo: str, gh_bin: str, env: dict
) -> tuple[dict | None, str]:
    """Issue #1647 Scope Delta AC7: download the actual artifact archive,
    recompute its sha256, and confirm it matches the receipt's
    ``artifact_archive_digest``. Also parses the single execution-record
    file inside the archive (mirrors
    scripts/agent-ops/test_verdict_execution_record_producer.py's
    _cmd_download_artifact) and cross-checks its self-reported
    ``payload_sha256`` against the receipt's ``execution_payload_sha256``
    (AC5/AC6). Returns the parsed record (which carries ``per_ac``
    coverage used to render the published body, AC8) or an error."""
    import io
    import zipfile

    artifact = receipt.get("execution_artifact") or {}
    artifact_id = artifact.get("artifact_id")
    expected_digest = artifact.get("artifact_archive_digest")
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
            ],
            capture_output=True,
            timeout=30,
            shell=False,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_api_artifact_download_exception: {exc}"
    if out.returncode != 0:
        stderr = (
            out.stderr.decode("utf-8", errors="replace")
            if isinstance(out.stderr, bytes)
            else (out.stderr or "")
        )
        return None, _classify_gh_error("gh_api_artifact_download_failed", stderr)
    data = out.stdout
    computed_digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if computed_digest != expected_digest:
        return None, "test_verdict_publish_receipt_artifact_archive_digest_mismatch"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        if len(names) != 1:
            return None, "test_verdict_publish_receipt_artifact_archive_unexpected_file_count"
        record = json.loads(archive.read(names[0]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"test_verdict_publish_receipt_artifact_archive_parse_exception: {exc}"
    if not isinstance(record, dict):
        return None, "test_verdict_publish_receipt_artifact_archive_record_invalid"
    reported_payload_sha256 = record.get("payload_sha256")
    stripped_record = dict(record)
    stripped_record.pop("payload_sha256", None)
    if _canonical_sha256(stripped_record) != reported_payload_sha256:
        return None, "test_verdict_publish_receipt_artifact_archive_payload_self_check_failed"
    if reported_payload_sha256 != receipt.get("execution_payload_sha256"):
        return None, "test_verdict_publish_receipt_artifact_archive_payload_sha256_mismatch"
    return record, ""


_TEST_VERDICT_BODY_PR_RE = _re.compile(r"^target_pr_number: (\d+)$", _re.MULTILINE)
_TEST_VERDICT_BODY_ISSUE_RE = _re.compile(r"^linked_issue_number: (\d+)$", _re.MULTILINE)
_TEST_VERDICT_BODY_HEAD_RE = _re.compile(r"^head_sha: ([0-9a-f]{40})$", _re.MULTILINE)
_TEST_VERDICT_BODY_ARTIFACT_RE = _re.compile(r"^artifact_id: (\d+)$", _re.MULTILINE)
_TEST_VERDICT_BODY_RESULT_RE = _re.compile(r"^result: (PASS|FAIL)$", _re.MULTILINE)
_TEST_VERDICT_BODY_AC_RE = _re.compile(r"^  - ac: (\S+)$", _re.MULTILINE)


def _render_test_verdict_body(
    receipt: dict,
    target_pr_number: int,
    linked_issue_number: int,
    expected_head_sha: str,
    artifact_id: int,
    per_ac_coverage: list,
) -> str:
    """Issue #1647 Scope Delta AC8: the published comment body is rendered
    deterministically from the verified receipt. No free-form caller body
    string is ever accepted (see _TEST_VERDICT_PUBLISH_ALLOWED_KEYS)."""
    result = "PASS" if receipt.get("pass_eligible") is True else "FAIL"
    lines = [
        "TEST_VERDICT_MACHINE/v2",
        f"target_pr_number: {target_pr_number}",
        f"linked_issue_number: {linked_issue_number}",
        f"head_sha: {expected_head_sha}",
        f"artifact_id: {artifact_id}",
        f"result: {result}",
    ]
    acs = sorted(
        {
            str(entry.get("ac"))
            for entry in (per_ac_coverage or [])
            if isinstance(entry, dict) and entry.get("ac")
        }
    )
    if acs:
        lines.append("per_ac_coverage:")
        for ac in acs:
            lines.append(f"  - ac: {ac}")
    return "\n".join(lines)


def _cross_check_test_verdict_body(
    body: str,
    *,
    target_pr_number: int,
    linked_issue_number: int,
    expected_head_sha: str,
    artifact_id: int,
    pass_eligible: bool,
    per_ac_coverage: list,
) -> str:
    """Issue #1647 Scope Delta AC8: defense-in-depth cross-check that the
    rendered/candidate body's embedded PR/Issue/HEAD/artifact/result/
    per-AC-coverage fields match the verified receipt's values. Used both
    as a self-check on the deterministically rendered body and as an
    independently testable validator."""
    match = _TEST_VERDICT_BODY_PR_RE.search(body)
    if not match or int(match.group(1)) != target_pr_number:
        return "test_verdict_publish_body_pr_number_mismatch"
    match = _TEST_VERDICT_BODY_ISSUE_RE.search(body)
    if not match or int(match.group(1)) != linked_issue_number:
        return "test_verdict_publish_body_issue_number_mismatch"
    match = _TEST_VERDICT_BODY_HEAD_RE.search(body)
    if not match or match.group(1) != expected_head_sha:
        return "test_verdict_publish_body_head_sha_mismatch"
    match = _TEST_VERDICT_BODY_ARTIFACT_RE.search(body)
    if not match or int(match.group(1)) != artifact_id:
        return "test_verdict_publish_body_artifact_id_mismatch"
    expected_result = "PASS" if pass_eligible else "FAIL"
    match = _TEST_VERDICT_BODY_RESULT_RE.search(body)
    if not match or match.group(1) != expected_result:
        return "test_verdict_publish_body_result_mismatch"
    expected_acs = sorted(
        {
            str(entry.get("ac"))
            for entry in (per_ac_coverage or [])
            if isinstance(entry, dict) and entry.get("ac")
        }
    )
    found_acs = sorted(_TEST_VERDICT_BODY_AC_RE.findall(body))
    if found_acs != expected_acs:
        return "test_verdict_publish_body_per_ac_coverage_mismatch"
    return ""


def _run_test_verdict_publish(args, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_test_verdict_publish_fields(input_data, args.repo, args.issue_number)
    if field_err:
        return _fail(field_err)
    if args.dry_run:
        return _ok({"status_detail": "dry_run_ok"})

    pr_number = input_data["target_pr_number"]
    expected_head_sha = input_data["expected_head_sha"]
    linked_issue_number = input_data["linked_issue_number"]
    receipt = input_data["producer_receipt"]
    artifact_id = receipt["execution_artifact"]["artifact_id"]
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )
    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "test_verdict_publish.marker.json"
    )
    gh_env = _build_pr_review_gh_env()
    # Issue #2163: pre-mutation metadata snapshot captured before any remote
    # POST is attempted, threaded into the two postcondition checks below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="failed",
        )

    def _write_marker(comment: dict) -> None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "schema": "TEST_VERDICT_PUBLISH_MARKER_V1",
                    "repo": args.repo,
                    "target_pr_number": pr_number,
                    "linked_issue_number": linked_issue_number,
                    "expected_head_sha": expected_head_sha,
                    "receipt_sha256": input_data["receipt_sha256"],
                    "idempotency_key": input_data["idempotency_key"],
                    "comment_id": comment["id"],
                    "comment_url": comment["html_url"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    marker = _test_verdict_marker_str(input_data["idempotency_key"])

    matches, match_err = _find_test_verdict_marker_matches(
        marker, pr_number, args.repo, gh_bin, gh_env
    )
    if match_err:
        return _fail(f"test_verdict_marker_precheck_failed: {match_err}", status="failed")
    if len(matches) > 1:
        return _fail("test_verdict_duplicate_marker_conflict_pre_mutation", status="failed")

    # Issue #1647 Scope Delta AC6/AC7: live GitHub Actions readback of the
    # receipt's producer/workflow-run/job/check-run identity and the
    # execution-record artifact, before any mutation is attempted.
    live_err = _verify_producer_run_and_job(receipt, args.repo, expected_head_sha, gh_bin, gh_env)
    if live_err:
        return _fail(
            f"test_verdict_publish_receipt_live_readback_failed: {live_err}", status="failed"
        )
    meta_err = _verify_execution_artifact_metadata(receipt, args.repo, gh_bin, gh_env)
    if meta_err:
        return _fail(
            f"test_verdict_publish_receipt_live_readback_failed: {meta_err}", status="failed"
        )
    record, archive_err = _download_and_verify_artifact_archive(receipt, args.repo, gh_bin, gh_env)
    if archive_err:
        return _fail(
            f"test_verdict_publish_receipt_live_readback_failed: {archive_err}", status="failed"
        )
    per_ac_coverage = record.get("per_ac") if isinstance(record, dict) else []

    # Issue #1647 Scope Delta AC8: deterministic body rendering from the
    # verified receipt, then a self cross-check (defense in depth).
    rendered_body = _render_test_verdict_body(
        receipt, pr_number, linked_issue_number, expected_head_sha, artifact_id, per_ac_coverage
    )
    cross_check_err = _cross_check_test_verdict_body(
        rendered_body,
        target_pr_number=pr_number,
        linked_issue_number=linked_issue_number,
        expected_head_sha=expected_head_sha,
        artifact_id=artifact_id,
        pass_eligible=receipt.get("pass_eligible") is True,
        per_ac_coverage=per_ac_coverage,
    )
    if cross_check_err:
        return _fail(cross_check_err, status="failed")
    if len(rendered_body.encode("utf-8")) > _TEST_VERDICT_BODY_MAX_BYTES:
        return _fail("test_verdict_publish_body_too_large", status="failed")

    # Issue #1647 Scope Delta AC10: reject if the rendered body already
    # contains the marker literal before it is appended and POSTed.
    if TEST_VERDICT_MARKER_PREFIX in rendered_body:
        return _fail("test_verdict_publish_marker_preembedded_in_body", status="failed")

    body_sha256 = hashlib.sha256(rendered_body.encode("utf-8")).hexdigest()
    full_body = f"{rendered_body}\n\n{marker}\n"

    current_head, head_err = _fetch_pr_head_sha(pr_number, args.repo, gh_bin, env=gh_env)
    if head_err:
        return _fail(head_err, status="failed")
    linked_body_sha, issue_err = _fetch_linked_issue_body_sha256(
        linked_issue_number, args.repo, gh_bin, gh_env
    )
    if issue_err:
        return _fail(issue_err, status="failed")
    if current_head != expected_head_sha:
        return _fail("test_verdict_pre_publish_head_mismatch", status="failed")
    if linked_body_sha != input_data["linked_issue_body_sha256"]:
        return _fail("test_verdict_pre_publish_linked_issue_body_mismatch", status="failed")
    authenticated_login, login_err = _fetch_authenticated_login(gh_bin, env=gh_env)
    if login_err:
        return _fail(login_err, status="failed")

    if len(matches) == 1:
        comment, readback_err = _readback_test_verdict_comment(
            matches[0].get("id"), args.repo, gh_bin, gh_env
        )
        if readback_err:
            return _fail(readback_err, status="failed")
        post_err = _validate_test_verdict_comment(comment, marker, body_sha256, authenticated_login)
        if post_err:
            return _fail(post_err, status="failed")
        # Issue #1647 Scope Delta AC9: idempotent retry must not rely on the
        # readback taken before this branch was known -- re-check PR HEAD and
        # linked Issue body for drift that may have occurred while this
        # comment/postcondition check ran, and re-confirm no tracked source
        # changes leaked out of the write root.
        retry_head, retry_head_err = _fetch_pr_head_sha(pr_number, args.repo, gh_bin, env=gh_env)
        retry_body_sha, retry_issue_err = _fetch_linked_issue_body_sha256(
            linked_issue_number, args.repo, gh_bin, gh_env
        )
        if (
            retry_head_err
            or retry_issue_err
            or retry_head != expected_head_sha
            or retry_body_sha != input_data["linked_issue_body_sha256"]
        ):
            return _fail(
                f"published_but_stale: current_head={retry_head} expected_head={expected_head_sha}",
                status="published_but_stale",
            )
        changed = _check_no_tracked_changes(
            PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
        )
        if changed:
            return _fail(
                "postcondition_tracked_changes_detected",
                [f"changed: {f}" for f in changed[:20]],
                status="failed",
            )
        _write_marker(comment)
        return _ok(
            {
                "status_detail": "already_published",
                "comment_id": comment["id"],
                "comment_url": comment["html_url"],
                "idempotency_marker_written": True,
            }
        )

    posted, post_err = _post_test_verdict_comment(pr_number, args.repo, full_body, gh_bin, gh_env)
    if post_err:
        return _fail(post_err, status="failed")
    if not isinstance(posted, dict) or not posted.get("id"):
        return _fail("test_verdict_post_response_missing_id", status="failed")
    comment, readback_err = _readback_test_verdict_comment(posted["id"], args.repo, gh_bin, gh_env)
    if readback_err:
        return _fail(readback_err, [f"posted_comment_id: {posted['id']}"], status="failed")
    postcondition_err = _validate_test_verdict_comment(
        comment, marker, body_sha256, authenticated_login
    )
    if postcondition_err:
        return _fail(postcondition_err, [f"posted_comment_id: {posted['id']}"], status="failed")

    # Issue #1647 Scope Delta AC10: this is best-effort duplicate detection
    # only (not a substitute for a real distributed exactly-once guarantee --
    # see the Safety Claim Matrix "not_controlled" entry). Re-list markers
    # after POST; anything other than exactly one match means a concurrent
    # publisher may have raced this one.
    post_matches, post_match_err = _find_test_verdict_marker_matches(
        marker, pr_number, args.repo, gh_bin, gh_env
    )
    if post_match_err or len(post_matches) != 1:
        _write_marker(comment)
        return _fail(
            f"test_verdict_publish_post_marker_recheck_failed: {post_match_err}",
            [f"posted_comment_id: {posted['id']}"],
            status="published_but_conflicted",
        )

    post_head, post_head_err = _fetch_pr_head_sha(pr_number, args.repo, gh_bin, env=gh_env)
    post_body_sha, post_issue_err = _fetch_linked_issue_body_sha256(
        linked_issue_number, args.repo, gh_bin, gh_env
    )
    if (
        post_head_err
        or post_issue_err
        or post_head != expected_head_sha
        or post_body_sha != input_data["linked_issue_body_sha256"]
    ):
        _write_marker(comment)
        return _fail(
            "test_verdict_published_but_precondition_drifted",
            [f"posted_comment_id: {posted['id']}"],
            status="published_but_stale",
        )
    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
    )
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )
    _write_marker(comment)
    return _ok(
        {
            "comment_id": comment["id"],
            "comment_url": comment["html_url"],
            "expected_head_sha": expected_head_sha,
            "receipt_sha256": input_data["receipt_sha256"],
            "idempotency_marker_written": True,
        }
    )


_ISSUECOMMENT_ID_RE = _re.compile(r"#issuecomment-(\d+)$")
_CANONICAL_SINGLE_COMMENT_PROJECTION = (
    "{id, html_url, created_at, updated_at, body, "
    "author: .user.login, author_id: .user.id, "
    "author_type: .user.type, author_association}"
)


def _extract_comment_id_from_url(url: str) -> str | None:
    """Extract the numeric comment id from a GitHub `#issuecomment-<id>` URL."""
    if not url:
        return None
    m = _ISSUECOMMENT_ID_RE.search(url)
    if not m:
        return None
    return m.group(1)


def _fetch_single_comment_by_id(comment_id: str, repo: str, gh_bin: str) -> dict:
    """Fetch exactly one comment by id (not a marker search across all comments)."""
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/comments/{comment_id}",
                "--jq",
                _CANONICAL_SINGLE_COMMENT_PROJECTION,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=_build_metadata_sanitized_env(),
        )
        if out.returncode != 0:
            return {"error": f"comment_fetch_failed_rc_{out.returncode}"}
        return {"comment": json.loads(out.stdout)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"comment_fetch_exception:{exc}"}


def _readback_contract_snapshot(
    marker_literal: str,
    issue_number: int,
    repo: str,
    gh_bin: str,
    expected_url: str,
    expected_body_sha256: str,
) -> dict:
    """Verify the posted snapshot against remote comment state, not child stdout.

    Issue #1459 review Blocker (legacy_refresh_duplicate_marker_deadlock): the
    idempotency marker is derived from (issue, body_sha256, schema) only, so a
    stale legacy go comment can share the exact same marker text as the fresh
    comment the publisher just posted. Searching all comments for a *unique*
    marker match therefore deadlocks permanently once both comments coexist.
    This function instead selects the single comment the publisher itself
    reported posting (by the comment id parsed from expected_url / html_url)
    and verifies marker/YAML/is_go_current against that one comment only.
    Global marker uniqueness across the whole comment list is not required.
    """
    try:
        comment_id = _extract_comment_id_from_url(expected_url)
        if not comment_id:
            return {"error": "contract_snapshot_url_missing_comment_id"}

        fetched = _fetch_single_comment_by_id(comment_id, repo, gh_bin)
        if "error" in fetched:
            return {"error": fetched["error"]}
        comment = fetched["comment"]
        if comment.get("html_url") != expected_url:
            return {"error": "remote_contract_snapshot_url_mismatch"}
        body = comment.get("body", "") or ""
        if marker_literal not in body:
            return {"error": "expected_contract_marker_not_embedded_in_selected_comment"}

        import importlib.util

        parser_path = (
            PROJECT_ROOT
            / ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py"
        )
        parser_spec = importlib.util.spec_from_file_location(
            "contract_review_result_parser", parser_path
        )
        ensure_path = PROJECT_ROOT / _ENSURE_CONTRACT_SNAPSHOT_REL
        ensure_spec = importlib.util.spec_from_file_location(
            "ensure_contract_snapshot", ensure_path
        )
        if not parser_spec or not parser_spec.loader or not ensure_spec or not ensure_spec.loader:
            return {"error": "contract_snapshot_readback_import_error"}
        parser_mod = importlib.util.module_from_spec(parser_spec)
        parser_spec.loader.exec_module(parser_mod)
        ensure_mod = importlib.util.module_from_spec(ensure_spec)
        ensure_spec.loader.exec_module(ensure_mod)
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
        results = parser_mod.parse_contract_review_results([comment], issue_url)
        # #1475 fix_delta P1 item 3: this is the actual controlled mutation
        # boundary. It must apply the same trusted_only=True gate as every
        # other consumer -- an untrusted comment must never be treated as an
        # authoritative snapshot readback here either.
        authoritative_go = getattr(parser_mod, "find_latest_authoritative_go", None)
        if callable(authoritative_go):
            snapshot = authoritative_go(results)
        else:
            try:
                snapshot = parser_mod.find_latest_go(
                    results, trusted_only=True, fingerprint_ready_only=True
                )
            except TypeError:  # legacy test-double only; production parser has the predicate
                snapshot = parser_mod.find_latest_go(results, trusted_only=True)
        if snapshot is None or not ensure_mod.is_go_current(snapshot, expected_body_sha256):
            return {"error": "remote_contract_snapshot_not_current"}

        # -- Issue #1459 review Blocker (post_publish_live_body_not_revalidated) --
        # The checks above only prove the *posted comment* is bound to
        # expected_body_sha256. They do not prove the *live* Issue body still
        # matches that hash at readback time -- a concurrent body edit between
        # the pre-publish check and this readback must not be reported as
        # success. Re-fetch the live body and require it to match the input
        # hash, the outer (comment-bound) hash, and the nested product-spec
        # hash carried inside the just-verified snapshot -- all three must
        # agree, not just the outer one.
        live_body, _live_updated_at, live_body_err = _fetch_issue_body_and_updated_at(
            issue_number, repo, gh_bin
        )
        if live_body_err:
            return {
                "error": f"failed_after_mutation:live_body_refetch_error:{live_body_err}",
                "comment_id": comment.get("id", ""),
                "comment_url": comment.get("html_url", ""),
            }
        live_body_sha256 = "sha256:" + hashlib.sha256((live_body or "").encode("utf-8")).hexdigest()
        inner = snapshot.get("inner") if isinstance(snapshot, dict) else None
        checks = inner.get("checks") if isinstance(inner, dict) else None
        product_spec_check = checks.get("product_spec_check") if isinstance(checks, dict) else None
        nested_product_spec_sha256 = (
            product_spec_check.get("body_sha256") if isinstance(product_spec_check, dict) else None
        )
        hashes_to_check = {
            "expected_body_sha256": expected_body_sha256,
            "live_body_sha256": live_body_sha256,
            "nested_product_spec_body_sha256": nested_product_spec_sha256,
        }
        if len(set(hashes_to_check.values())) != 1:
            return {
                "error": (
                    f"failed_after_mutation:live_body_hash_mismatch:{json.dumps(hashes_to_check, sort_keys=True)}"
                ),
                "comment_id": comment.get("id", ""),
                "comment_url": comment.get("html_url", ""),
            }

        return {
            "comment_id": comment.get("id", ""),
            "comment_url": comment.get("html_url", ""),
            "remote_postcondition_verified": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"remote_contract_snapshot_readback_exception:{exc}"}


def _run_contract_snapshot_publish(args, input_data, gh_bin, _fail, _ok) -> int:
    # -- Blocker 4: input schema binding (repo / target_issue_body_sha256 /
    # expected_latest_contract_review_status / expected_contract_marker /
    # operation_reason) — an under-specified input can no longer launch
    # ensure_contract_snapshot.py --mode auto --post.
    field_err = _validate_contract_snapshot_publish_fields(input_data, args.repo)
    if field_err:
        return _fail(field_err)

    # -- Blocker 5: publisher module chain realpath / shadowing check.
    realpath_errors = _check_contract_snapshot_module_realpaths(PROJECT_ROOT)
    if realpath_errors:
        return _fail("module_shadowing_detected", realpath_errors)

    if args.dry_run:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "dry_run_ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # -- target_issue_body_sha256 precondition: refuse to publish a contract
    # snapshot against an Issue body that has moved since the caller computed
    # its expected state.
    body, _updated_at, body_err = _fetch_issue_body_and_updated_at(
        args.issue_number, args.repo, gh_bin
    )
    if body_err:
        return _fail(body_err, status="failed")
    current_body_sha256 = "sha256:" + hashlib.sha256((body or "").encode("utf-8")).hexdigest()
    if current_body_sha256 != input_data["target_issue_body_sha256"]:
        return _fail(
            f"target_issue_body_sha256_mismatch: current={current_body_sha256} "
            f"expected={input_data['target_issue_body_sha256']}",
            status="failed",
        )

    publisher = PROJECT_ROOT / _ENSURE_CONTRACT_SNAPSHOT_REL
    if not publisher.exists():
        return _fail(f"publisher_missing: {publisher}", status="failed")

    artifact_dir = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, ""
    ).parent
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )
    # Issue #2163: pre-mutation metadata snapshot captured before the
    # ensure_contract_snapshot.py subprocess (which performs the remote POST)
    # is launched, threaded into the postcondition check below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="failed",
        )
    cmd = [
        sys.executable,
        str(publisher),
        "--issue-number",
        str(args.issue_number),
        "--repo",
        args.repo,
        "--mode",
        "auto",
        "--post",
        "--artifact-dir",
        str(artifact_dir),
    ]
    # -- Blocker 5: sanitized env (PYTHONPATH / PYTHONHOME / editor / browser /
    # prompt overrides removed), same boundary as _build_sanitized_env().
    sanitized_env = _build_metadata_sanitized_env()
    try:
        proc = subprocess.run(  # noqa: PLW1510
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(PROJECT_ROOT),
            shell=False,
            env=sanitized_env,
        )
    except subprocess.TimeoutExpired:
        return _fail("publisher_timeout_180s", status="failed")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"publisher_launch_error: {exc}", status="failed")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return _fail("publisher_no_stdout", [proc.stderr[:500]], status="failed")
    try:
        pub_result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _fail(f"publisher_json_parse_error: {exc}", status="failed")

    pub_status = pub_result.get("status")
    if pub_status != "ok" or not pub_result.get("contract_snapshot_url"):
        return _fail(
            f"publisher_did_not_succeed: status={pub_status!r}",
            pub_result.get("errors") or [f"post_status={pub_result.get('post_status')}"],
            status="failed",
        )

    readback = _readback_contract_snapshot(
        input_data["expected_contract_marker"],
        args.issue_number,
        args.repo,
        gh_bin,
        pub_result["contract_snapshot_url"],
        input_data["target_issue_body_sha256"],
    )
    if "error" in readback:
        # The publisher's POST already happened (a remote side effect exists).
        # Preserve the posted URL/comment id as evidence even on failure so the
        # caller can locate and reconcile the mutation instead of only seeing
        # an opaque error string.
        evidence = [readback["error"]]
        if readback.get("comment_url"):
            evidence.append(f"posted_comment_url: {readback['comment_url']}")
        if readback.get("comment_id"):
            evidence.append(f"posted_comment_id: {readback['comment_id']}")
        return _fail(readback["error"], evidence, status="failed")

    # -- AC14 / Blocker 6: postcondition -- no changes outside this command's
    # own write root (artifacts/{issue}/issue-metadata/contract_snapshot.publish/).
    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
    )
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    return _ok(
        {
            "contract_snapshot_url": readback["comment_url"],
            "post_status": pub_result.get("post_status"),
            "remote_postcondition_verified": True,
            "idempotency_marker_written": False,
        }
    )


# -- Issue #1632: controlled removal of a stale closed-blocker GitHub native
# `blockedBy` relationship (issue_dependency.remove) -----------------------
#
# Fixed GraphQL host/query/mutation. No caller-supplied query, host, argv,
# credential, or response path. Every read is an exhaustive all-page
# readback (pageInfo.hasNextPage must reach false); mutation happens at
# most once per invocation (no automatic retry on transport/GraphQL error);
# and a fresh all-page readback is required both BEFORE (precondition) and
# AFTER (postcondition) the single removeBlockedBy call.

_ISSUE_DEPENDENCY_REMOVE_BLOCKED_BY_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      state
      blockedBy(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id number state repository { nameWithOwner } }
      }
    }
  }
}
"""

# Issue #1667 review fix_delta P0: the official GitHub GraphQL schema names
# the RemoveBlockedByInput field `blockingIssueId` (NOT `blockedByIssueId` --
# that name never existed on the input type; see
# docs.github.com/public/fpt/schema.docs.graphql). clientMutationId is threaded
# through as the caller-declared idempotency_key so the response can be
# cross-checked against the exact request that produced it (P1).
_ISSUE_DEPENDENCY_REMOVE_MUTATION = """
mutation($issueId: ID!, $blockingIssueId: ID!, $clientMutationId: String) {
  removeBlockedBy(input: {issueId: $issueId, blockingIssueId: $blockingIssueId, clientMutationId: $clientMutationId}) {
    issue { id number }
    blockingIssue { id number }
    clientMutationId
  }
}
"""

_ISSUE_DEPENDENCY_REMOVE_TRUSTED_PERMISSIONS = frozenset({"admin", "write", "maintain"})

# Hard bound on pagination loop iterations, independent of the caller-declared
# expected_blocked_by_numbers size cap -- prevents a runaway loop even if a
# malformed/adversarial response never sets hasNextPage to false.
_ISSUE_DEPENDENCY_REMOVE_MAX_PAGES = 50


def _build_issue_dependency_remove_gh_env() -> dict[str, str]:
    """Sanitized environment for every `gh` subprocess call made while
    removing an issue dependency relationship. Strips the generic
    ENV_SANITIZE_KEYS plus GH_HOST/GH_REPO/GH_CONFIG_DIR/GH_DEBUG/DEBUG,
    the same boundary already used by _build_pr_review_gh_env()."""
    env = os.environ.copy()
    for key in ENV_SANITIZE_KEYS:
        env.pop(key, None)
    for key in ("GH_HOST", "GH_REPO", "GH_CONFIG_DIR", "GH_DEBUG", "DEBUG"):
        env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _graphql_call(
    gh_bin: str, env: dict[str, str], query: str, variables: dict
) -> tuple[dict | None, str]:
    """Execute a single fixed-host GraphQL call via `gh api graphql --input -`.

    Never uses shell-interpolated -f/-F flags for the query text (uses
    `--input -` so query/variables round-trip as an exact JSON POST body).
    Returns (data, error); data is the `data` object of
    the parsed GraphQL response, or None on any transport/schema/GraphQL
    `errors` failure.
    """
    payload = json.dumps({"query": query, "variables": variables})
    try:
        out = subprocess.run(  # noqa: PLW1510
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST, "graphql", "--input", "-"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_graphql_failed", out.stderr or "")
        try:
            parsed = json.loads(out.stdout)
        except Exception as exc:  # noqa: BLE001
            return (
                None,
                f"gh_api_graphql_response_parse_error: {_redact_secret_like_tokens(str(exc))}",
            )
        if not isinstance(parsed, dict):
            return None, "gh_api_graphql_response_not_object"
        if parsed.get("errors"):
            safe_errors = _redact_secret_like_tokens(json.dumps(parsed["errors"]))[:300]
            return None, f"gh_api_graphql_errors: {safe_errors}"
        data = parsed.get("data")
        if not isinstance(data, dict):
            return None, "gh_api_graphql_response_missing_data"
        return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"gh_api_graphql_exception: {_redact_secret_like_tokens(str(exc))}"


def _fetch_issue_dependency_remove_actor(
    gh_bin: str, env: dict[str, str], repo: str
) -> tuple[str | None, str | None, str]:
    """Fetch (login, permission, error) for the authenticated gh identity
    against `repo`. Never records the token/credential itself -- only the
    login and the coarse permission string are ever returned/recorded."""
    login, err = _fetch_authenticated_login(gh_bin, env=env)
    if err:
        return None, None, err
    try:
        out = subprocess.run(  # noqa: PLW1510
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/collaborators/{login}/permission",
                "--jq",
                ".permission",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            env=env,
        )
        if out.returncode != 0:
            return (
                login,
                None,
                _classify_gh_error("gh_api_permission_fetch_failed", out.stderr or ""),
            )
        permission = out.stdout.strip()
        if not permission:
            return login, None, "gh_api_permission_empty"
        return login, permission, ""
    except Exception as exc:  # noqa: BLE001
        return login, None, f"gh_api_permission_exception: {exc}"


def _fetch_blocked_by_all_pages(
    issue_number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[dict | None, str]:
    """Exhaustive cursor-paginated readback of Issue.blockedBy.

    Returns (result, error). result = {blocked_issue_id, blocked_issue_number,
    blocked_issue_state, nodes: [{id, number, state}], page_count}. Fail-closed
    on: GraphQL errors, missing/malformed response shape, cross-page identity
    drift, non-repo nodes, duplicate node ids/numbers across pages, a cursor
    that does not progress while hasNextPage is true, and the caller-declared
    size cap being exceeded.
    """
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return None, "repo_slug_malformed"

    nodes: list[dict] = []
    seen_numbers: set[int] = set()
    seen_ids: set[str] = set()
    cursor = None
    page_count = 0
    blocked_issue_id = None
    blocked_issue_number = None
    blocked_issue_state = None

    while True:
        data, err = _graphql_call(
            gh_bin,
            env,
            _ISSUE_DEPENDENCY_REMOVE_BLOCKED_BY_QUERY,
            {"owner": owner, "name": name, "number": issue_number, "cursor": cursor},
        )
        if err:
            return None, err

        repository = data.get("repository")
        if not isinstance(repository, dict):
            return None, "graphql_response_missing_repository"
        issue = repository.get("issue")
        if not isinstance(issue, dict):
            return None, "graphql_response_missing_issue"

        if blocked_issue_id is None:
            blocked_issue_id = issue.get("id")
            blocked_issue_number = issue.get("number")
            blocked_issue_state = issue.get("state")
            if not isinstance(blocked_issue_id, str) or not blocked_issue_id:
                return None, "graphql_response_blocked_issue_id_invalid"
            if blocked_issue_number != issue_number:
                return None, "graphql_response_blocked_issue_number_mismatch"
        elif issue.get("id") != blocked_issue_id or issue.get("number") != blocked_issue_number:
            return None, "graphql_response_blocked_issue_identity_drift_mid_pagination"

        blocked_by = issue.get("blockedBy")
        if not isinstance(blocked_by, dict):
            return None, "graphql_response_missing_blocked_by"
        page_info = blocked_by.get("pageInfo")
        if not isinstance(page_info, dict):
            return None, "graphql_response_missing_page_info"
        page_nodes = blocked_by.get("nodes")
        if not isinstance(page_nodes, list):
            return None, "graphql_response_missing_nodes"

        page_count += 1
        if page_count > _ISSUE_DEPENDENCY_REMOVE_MAX_PAGES:
            return None, "graphql_pagination_runaway"

        for node in page_nodes:
            if not isinstance(node, dict):
                return None, "graphql_response_node_not_object"
            node_id = node.get("id")
            node_number = node.get("number")
            node_state = node.get("state")
            node_repo = (node.get("repository") or {}).get("nameWithOwner")
            if not isinstance(node_id, str) or not node_id:
                return None, "graphql_response_node_id_invalid"
            if type(node_number) is not int or node_number <= 0:
                return None, "graphql_response_node_number_invalid"
            if node_state not in ("OPEN", "CLOSED"):
                return None, f"graphql_response_node_state_invalid: {node_state!r}"
            if node_repo != repo:
                return None, f"graphql_response_node_repo_mismatch: {node_repo!r}"
            if node_number in seen_numbers or node_id in seen_ids:
                return None, "graphql_response_duplicate_node_across_pages"
            seen_numbers.add(node_number)
            seen_ids.add(node_id)
            nodes.append({"id": node_id, "number": node_number, "state": node_state})
            if len(nodes) > ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS:
                return None, "graphql_response_blocked_by_size_cap_exceeded"

        has_next = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next, bool):
            return None, "graphql_response_has_next_page_not_bool"
        if has_next:
            if not isinstance(end_cursor, str) or not end_cursor or end_cursor == cursor:
                return None, "graphql_response_cursor_invalid_or_not_progressing"
            cursor = end_cursor
            continue
        break

    return {
        "blocked_issue_id": blocked_issue_id,
        "blocked_issue_number": blocked_issue_number,
        "blocked_issue_state": blocked_issue_state,
        "nodes": nodes,
        "page_count": page_count,
    }, ""


def _compute_blocked_by_snapshot_sha256(
    blocked_issue_id: str, blocked_issue_number: int, nodes: list[dict]
) -> str:
    """Deterministic hash binding blocked-issue identity + the full sorted
    (number, id, state) set of its blockedBy relationships."""
    canonical_nodes = sorted(
        ({"id": n["id"], "number": n["number"], "state": n["state"]} for n in nodes),
        key=lambda n: n["number"],
    )
    payload = {
        "blocked_issue_id": blocked_issue_id,
        "blocked_issue_number": blocked_issue_number,
        "blocked_by": canonical_nodes,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# Issue #1667 review fix_delta P1: RemoveBlockedByPayload validator. The
# executor must not treat a 200-with-`data` GraphQL response as success
# without checking WHICH issue/blocker it actually mutated -- an id/number
# mismatch here means the mutation executed against an unexpected target
# (postcondition_rejected), while a missing/malformed shape means the
# response cannot be trusted to mean anything at all
# (transport_or_schema_error).
def _validate_remove_blocked_by_mutation_response(
    mutation_data: dict | None,
    *,
    expected_blocked_issue_node_id: str,
    expected_blocked_issue_number: int,
    expected_blocker_node_id: str,
    expected_blocker_number: int,
    expected_client_mutation_id: str,
) -> tuple[str, bool]:
    """Validate a removeBlockedBy GraphQL mutation response.

    Returns (error, is_schema_error). error == "" means the response is fully
    valid. is_schema_error=True means missing/malformed response shape
    (caller classifies as transport_or_schema_error); is_schema_error=False
    means a well-formed response whose values mismatch the caller-declared
    expectation (caller classifies as postcondition_rejected).
    """
    if not isinstance(mutation_data, dict):
        return "mutation_response_not_object", True
    payload = mutation_data.get("removeBlockedBy")
    if not isinstance(payload, dict):
        return "mutation_response_missing_remove_blocked_by_payload", True

    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return "mutation_response_missing_issue", True
    blocking_issue = payload.get("blockingIssue")
    if not isinstance(blocking_issue, dict):
        return "mutation_response_missing_blocking_issue", True

    issue_id = issue.get("id")
    issue_number = issue.get("number")
    blocking_id = blocking_issue.get("id")
    blocking_number = blocking_issue.get("number")
    if not isinstance(issue_id, str) or not issue_id:
        return "mutation_response_issue_id_invalid", True
    if type(issue_number) is not int:
        return "mutation_response_issue_number_invalid", True
    if not isinstance(blocking_id, str) or not blocking_id:
        return "mutation_response_blocking_issue_id_invalid", True
    if type(blocking_number) is not int:
        return "mutation_response_blocking_issue_number_invalid", True

    if issue_id != expected_blocked_issue_node_id or issue_number != expected_blocked_issue_number:
        return "mutation_response_issue_identity_mismatch", False
    if blocking_id != expected_blocker_node_id or blocking_number != expected_blocker_number:
        return "mutation_response_blocking_issue_identity_mismatch", False

    if "clientMutationId" not in payload:
        return "mutation_response_missing_client_mutation_id", True
    if payload.get("clientMutationId") != expected_client_mutation_id:
        return "mutation_response_client_mutation_id_mismatch", False

    return "", False


# Issue #1667 review fix_delta P2: closed-schema idempotency marker
# validator. A stored marker is only ever trusted as evidence of a prior
# successful removal when EVERY one of these fields matches the current
# caller-declared context exactly -- a partial/loose match (e.g. only
# idempotency_key) is never sufficient.
_ISSUE_DEPENDENCY_REMOVE_MARKER_SCHEMA = "ISSUE_DEPENDENCY_REMOVE_MARKER_V1"


def _validate_dependency_remove_marker(
    marker: object,
    *,
    issue_number: int,
    repo: str,
    target_blocker_number: int,
    expected_blocked_issue_node_id: str,
    expected_blocker_node_id: str,
    idempotency_key: str,
) -> str:
    """Return "" iff marker is a fully-matching ISSUE_DEPENDENCY_REMOVE_MARKER_V1
    recording a completed removal for this exact context, else a descriptive
    mismatch code."""
    if not isinstance(marker, dict):
        return "marker_not_object"
    if marker.get("schema") != _ISSUE_DEPENDENCY_REMOVE_MARKER_SCHEMA:
        return "marker_schema_mismatch"
    if marker.get("issue_number") != issue_number:
        return "marker_issue_number_mismatch"
    if marker.get("repo") != repo:
        return "marker_repo_mismatch"
    if marker.get("target_blocker_number") != target_blocker_number:
        return "marker_target_blocker_number_mismatch"
    if marker.get("blocked_issue_id") != expected_blocked_issue_node_id:
        return "marker_blocked_issue_id_mismatch"
    if marker.get("blocker_node_id") != expected_blocker_node_id:
        return "marker_blocker_node_id_mismatch"
    if marker.get("idempotency_key") != idempotency_key:
        return "marker_idempotency_key_mismatch"
    actor_login = marker.get("actor_login")
    if not isinstance(actor_login, str) or not actor_login:
        return "marker_actor_login_missing"
    if marker.get("status_detail") != "removed":
        return "marker_status_detail_not_removed"
    return ""


def _run_issue_dependency_remove(args, input_data, gh_bin, _fail, _ok) -> int:
    field_err = validate_issue_dependency_remove_input(input_data, args.issue_number, args.repo)
    if field_err:
        return _fail(field_err)

    target_blocker_number = input_data["target_blocker_number"]
    expected_blocked_issue_node_id = input_data["expected_blocked_issue_node_id"]
    expected_blocker_node_id = input_data["expected_blocker_node_id"]
    expected_numbers = input_data["expected_blocked_by_numbers"]
    expected_pre_hash = input_data["expected_pre_mutation_snapshot_sha256"]
    idempotency_key = input_data["idempotency_key"]

    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_dependency_remove.marker.json"
    )
    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )

    if args.dry_run:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "dry_run_ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    gh_env = _build_issue_dependency_remove_gh_env()

    # -- AC3: trusted credential actor readback. Runs before any relationship
    # read/mutation. Only login + coarse permission are ever recorded -- never
    # a token/credential.
    login, permission, actor_err = _fetch_issue_dependency_remove_actor(gh_bin, gh_env, args.repo)
    if actor_err:
        return _fail(actor_err, status="transport_or_schema_error")
    if permission not in _ISSUE_DEPENDENCY_REMOVE_TRUSTED_PERMISSIONS:
        return _fail(
            f"credential_actor_not_authorized: login={login!r} permission={permission!r}",
            status="precondition_rejected",
        )

    marker_write_errors: list[str] = []

    def _write_marker(
        status_detail: str,
        pre_hash: str | None,
        post_hash: str | None = None,
        *,
        blocked_issue_id: str | None = None,
        blocked_issue_number: int | None = None,
        blocker_node_id: str | None = None,
    ) -> None:
        # Issue #1667 review fix_delta P1: marker write failure is recorded
        # explicitly (marker_write_errors) rather than silently swallowed --
        # it is included in the result payload of whichever return path
        # triggered this write.
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema": _ISSUE_DEPENDENCY_REMOVE_MARKER_SCHEMA,
                        "issue_number": args.issue_number,
                        "repo": args.repo,
                        "target_blocker_number": target_blocker_number,
                        "blocked_issue_id": blocked_issue_id or expected_blocked_issue_node_id,
                        "blocked_issue_number": blocked_issue_number or args.issue_number,
                        "blocker_node_id": blocker_node_id or expected_blocker_node_id,
                        "idempotency_key": idempotency_key,
                        "actor_login": login,
                        "actor_permission": permission,
                        "pre_mutation_snapshot_sha256": pre_hash,
                        "post_mutation_snapshot_sha256": post_hash,
                        "status_detail": status_detail,
                        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except Exception as exc:  # noqa: BLE001
            marker_write_errors.append(f"marker_write_failed:{status_detail}:{exc}")

    existing_marker = None
    if marker_path.exists():
        try:
            existing_marker = json.loads(marker_path.read_text())
        except Exception:  # noqa: BLE001
            existing_marker = None

    # -- AC2 / precondition: all-page pre-mutation readback. Remote state is
    # the sole authority; a local marker never substitutes for it.
    pre_state, pre_err = _fetch_blocked_by_all_pages(args.issue_number, args.repo, gh_bin, gh_env)
    if pre_err:
        return _fail(pre_err, status="transport_or_schema_error")

    if pre_state["blocked_issue_id"] != expected_blocked_issue_node_id:
        return _fail("precondition_blocked_issue_node_id_mismatch", status="precondition_rejected")

    pre_numbers = sorted(n["number"] for n in pre_state["nodes"])

    # -- Idempotency (Issue #1667 review fix_delta P2): a FULLY validated
    # marker for this exact context plus a FRESH remote readback showing the
    # target relationship already absent is the only path to
    # already_completed. A marker that exists but fails closed-schema
    # validation is never trusted; if the target relationship also happens
    # to be absent, that ambiguous state is routed to human judgment
    # (postcondition_rejected) rather than silently treated as
    # already_completed.
    if existing_marker is not None and target_blocker_number not in pre_numbers:
        marker_validation_err = _validate_dependency_remove_marker(
            existing_marker,
            issue_number=args.issue_number,
            repo=args.repo,
            target_blocker_number=target_blocker_number,
            expected_blocked_issue_node_id=expected_blocked_issue_node_id,
            expected_blocker_node_id=expected_blocker_node_id,
            idempotency_key=idempotency_key,
        )
        if not marker_validation_err:
            computed_pre_hash = _compute_blocked_by_snapshot_sha256(
                pre_state["blocked_issue_id"], pre_state["blocked_issue_number"], pre_state["nodes"]
            )
            result = {
                "status": "already_completed",
                "actor_login": login,
                "actor_permission": permission,
                "pre_mutation_snapshot_sha256": computed_pre_hash,
                "idempotency_marker_found": True,
            }
            if marker_write_errors:
                result["marker_write_errors"] = list(marker_write_errors)
            return _ok(result)
        return _fail(
            f"already_completed_marker_invalid: {marker_validation_err}",
            status="postcondition_rejected",
        )

    if pre_numbers != expected_numbers:
        return _fail(
            f"precondition_blocked_by_set_mismatch: current={pre_numbers} expected={expected_numbers}",
            status="precondition_rejected",
        )

    target_nodes = [n for n in pre_state["nodes"] if n["number"] == target_blocker_number]
    if len(target_nodes) != 1:
        return _fail(
            "precondition_target_blocker_not_found_exactly_once", status="precondition_rejected"
        )
    target_node = target_nodes[0]
    if target_node["id"] != expected_blocker_node_id:
        return _fail("precondition_target_blocker_node_id_mismatch", status="precondition_rejected")
    if target_node["state"] != "CLOSED":
        return _fail("precondition_target_blocker_not_closed", status="precondition_rejected")

    computed_pre_hash = _compute_blocked_by_snapshot_sha256(
        pre_state["blocked_issue_id"], pre_state["blocked_issue_number"], pre_state["nodes"]
    )
    if computed_pre_hash != expected_pre_hash:
        return _fail(
            f"precondition_pre_mutation_snapshot_sha256_mismatch: computed={computed_pre_hash} "
            f"expected={expected_pre_hash}",
            status="precondition_rejected",
        )

    # -- Precondition (Issue #1667 review fix_delta P1): confirm no unrelated
    # tracked/staged/untracked changes exist BEFORE the remote mutation is
    # attempted, not only after.
    pre_mutation_changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if pre_mutation_changed:
        return _fail(
            "precondition_tracked_changes_detected",
            [f"changed: {f}" for f in pre_mutation_changed[:20]],
            status="precondition_rejected",
        )

    # Issue #2163: pre-mutation metadata snapshot captured at this same point
    # (immediately after the precondition above passes and before any remote
    # mutation is attempted), threaded into the postcondition check below.
    pre_mutation_snapshot, pre_mutation_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_snapshot_err is not None:
        # Issue #1667 review fix_delta P1: this command id's closed
        # result-status set never includes "failed" -- classify as
        # precondition_rejected, matching the sibling clean-state check above.
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_snapshot_err}",
            status="precondition_rejected",
        )

    # -- Attempt marker (Issue #1667 review fix_delta P1): written BEFORE the
    # remote mutation call so any post-mutation failure (including an
    # interpreter crash) still leaves an audit trail proving the mutation
    # was attempted.
    _write_marker(
        "mutation_attempted",
        computed_pre_hash,
        blocked_issue_id=pre_state["blocked_issue_id"],
        blocked_issue_number=pre_state["blocked_issue_number"],
        blocker_node_id=expected_blocker_node_id,
    )

    # -- AC4: single mutation attempt. No automatic retry on transport/GraphQL
    # error -- a failed call is recorded and reported, never retried here.
    _mutation_data, mutation_err = _graphql_call(
        gh_bin,
        gh_env,
        _ISSUE_DEPENDENCY_REMOVE_MUTATION,
        {
            "issueId": expected_blocked_issue_node_id,
            "blockingIssueId": expected_blocker_node_id,
            "clientMutationId": idempotency_key,
        },
    )
    if mutation_err:
        _write_marker(
            "transport_or_schema_error",
            computed_pre_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        errors = [mutation_err] + marker_write_errors
        return _fail(mutation_err, errors, status="transport_or_schema_error")

    # -- Issue #1667 review fix_delta P1: validate the removeBlockedBy
    # response shape/identity before trusting the mutation succeeded against
    # the intended target.
    response_err, response_is_schema_error = _validate_remove_blocked_by_mutation_response(
        _mutation_data,
        expected_blocked_issue_node_id=expected_blocked_issue_node_id,
        expected_blocked_issue_number=pre_state["blocked_issue_number"],
        expected_blocker_node_id=expected_blocker_node_id,
        expected_blocker_number=target_blocker_number,
        expected_client_mutation_id=idempotency_key,
    )
    if response_err:
        response_status = (
            "transport_or_schema_error" if response_is_schema_error else "postcondition_rejected"
        )
        _write_marker(
            response_status,
            computed_pre_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        return _fail(response_err, status=response_status)

    # -- AC5: all-page post-mutation readback (TOCTOU close-out).
    post_state, post_err = _fetch_blocked_by_all_pages(args.issue_number, args.repo, gh_bin, gh_env)
    if post_err:
        _write_marker(
            "transport_or_schema_error",
            computed_pre_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        return _fail(post_err, status="transport_or_schema_error")

    if post_state["blocked_issue_id"] != expected_blocked_issue_node_id:
        _write_marker(
            "postcondition_rejected",
            computed_pre_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        return _fail(
            "postcondition_blocked_issue_node_id_mismatch", status="postcondition_rejected"
        )

    post_numbers = sorted(n["number"] for n in post_state["nodes"])
    expected_post_numbers = sorted(n for n in expected_numbers if n != target_blocker_number)
    if target_blocker_number in post_numbers:
        _write_marker(
            "postcondition_rejected",
            computed_pre_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        return _fail(
            "postcondition_target_relationship_still_present", status="postcondition_rejected"
        )
    if post_numbers != expected_post_numbers:
        _write_marker(
            "postcondition_rejected",
            computed_pre_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        return _fail(
            f"postcondition_non_target_set_changed: current={post_numbers} expected={expected_post_numbers}",
            status="postcondition_rejected",
        )

    pre_by_number = {n["number"]: n["id"] for n in pre_state["nodes"]}
    for n in post_state["nodes"]:
        if pre_by_number.get(n["number"]) != n["id"]:
            _write_marker(
                "postcondition_rejected",
                computed_pre_hash,
                blocked_issue_id=pre_state["blocked_issue_id"],
                blocked_issue_number=pre_state["blocked_issue_number"],
                blocker_node_id=expected_blocker_node_id,
            )
            return _fail("postcondition_non_target_node_id_drift", status="postcondition_rejected")

    computed_post_hash = _compute_blocked_by_snapshot_sha256(
        post_state["blocked_issue_id"], post_state["blocked_issue_number"], post_state["nodes"]
    )

    # -- AC14-equivalent postcondition: no changes outside this command's own
    # write root. Issue #1667 review fix_delta P1: classified as
    # postcondition_rejected (not the undefined "failed" status) -- the
    # closed result-status set for this command id never includes "failed".
    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_snapshot
    )
    if changed:
        _write_marker(
            "postcondition_rejected",
            computed_pre_hash,
            computed_post_hash,
            blocked_issue_id=pre_state["blocked_issue_id"],
            blocked_issue_number=pre_state["blocked_issue_number"],
            blocker_node_id=expected_blocker_node_id,
        )
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="postcondition_rejected",
        )

    _write_marker(
        "removed",
        computed_pre_hash,
        computed_post_hash,
        blocked_issue_id=pre_state["blocked_issue_id"],
        blocked_issue_number=pre_state["blocked_issue_number"],
        blocker_node_id=expected_blocker_node_id,
    )

    result = {
        "status": "removed",
        "actor_login": login,
        "actor_permission": permission,
        "pre_mutation_snapshot_sha256": computed_pre_hash,
        "post_mutation_snapshot_sha256": computed_post_hash,
        "idempotency_marker_written": True,
    }
    if marker_write_errors:
        result["marker_write_errors"] = list(marker_write_errors)
    return _ok(result)


# -- Issue #1883: controlled native relationship (parent / blockedBy /
# blocking) synchronization (issue_relationship.update) -----------------------
#
# Fixed GraphQL host/queries/mutations. No caller-supplied query, host, argv,
# credential, or response path (AC13). Every read is an exhaustive all-page
# readback for blockedBy/blocking (pageInfo.hasNextPage must reach false);
# parent is a single-node read. Mutation happens as an ordered, sequential,
# no-automatic-retry saga (AC2/AC9/AC12 Required Transaction Order): the
# executor re-validates the caller-declared graph invariants (defense in
# depth, same validator as the pure input schema check), re-reads live state
# as the sole precondition authority (a caller-declared expected_before is
# never trusted without a fresh comparison), rejects an ancestor cycle before
# any parent mutation, executes each explicit add/remove operation with a
# fixed argv, and always performs a full post-readback (regardless of
# partial failure) so the caller can classify the encountered state.

_RELATIONSHIP_SELF_AND_PARENT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      state
      parent { id number state }
    }
  }
}
"""

_RELATIONSHIP_FIELD_QUERY_TEMPLATE = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      %FIELD%(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id number state repository { nameWithOwner } }
      }
    }
  }
}
"""

_ISSUE_NODE_LOOKUP_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { id number state }
  }
}
"""

_ADD_SUB_ISSUE_MUTATION = """
mutation($issueId: ID!, $subIssueId: ID!, $clientMutationId: String) {
  addSubIssue(input: {
    issueId: $issueId
    subIssueId: $subIssueId
    replaceParent: true
    clientMutationId: $clientMutationId
  }) {
    issue { id number }
    subIssue { id number }
  }
}
"""

_REMOVE_SUB_ISSUE_MUTATION = """
mutation($issueId: ID!, $subIssueId: ID!, $clientMutationId: String) {
  removeSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId, clientMutationId: $clientMutationId}) {
    issue { id number }
    subIssue { id number }
  }
}
"""

_RELATIONSHIP_ADD_BLOCKED_BY_MUTATION = """
mutation($issueId: ID!, $blockingIssueId: ID!, $clientMutationId: String) {
  addBlockedBy(input: {issueId: $issueId, blockingIssueId: $blockingIssueId, clientMutationId: $clientMutationId}) {
    issue { id number }
    blockingIssue { id number }
  }
}
"""

_RELATIONSHIP_REMOVE_BLOCKED_BY_MUTATION = """
mutation($issueId: ID!, $blockingIssueId: ID!, $clientMutationId: String) {
  removeBlockedBy(input: {issueId: $issueId, blockingIssueId: $blockingIssueId, clientMutationId: $clientMutationId}) {
    issue { id number }
    blockingIssue { id number }
  }
}
"""

_RELATIONSHIP_TRUSTED_PERMISSIONS = _ISSUE_DEPENDENCY_REMOVE_TRUSTED_PERMISSIONS
_RELATIONSHIP_MAX_PAGES = 50
_RELATIONSHIP_MAX_ANCESTOR_WALK = 50


def _relationship_gh_env() -> dict[str, str]:
    """Sanitized environment for every `gh` subprocess call made across the
    issue_relationship.update route -- actor verification, precondition
    readback, the fixed GraphQL relationship mutation, postcondition
    readback, and zero-delta no-op verification all share this single
    `env` value (see `_run_issue_relationship_update()`, which builds it
    once via this function and threads it through every call below).

    Issue #2665: this route previously delegated to
    `_build_issue_dependency_remove_gh_env()`, the higher-trust boundary
    #1667 built specifically for the issue-dependency-removal lane, which
    unconditionally strips GH_CONFIG_DIR (and GH_TOKEN/GITHUB_TOKEN via the
    generic `ENV_SANITIZE_KEYS`). That boundary is the wrong fit here: the
    live preflight/readback context that gates a relationship update (the
    issue-metadata read/write helpers this module also exposes) uses
    `_build_metadata_sanitized_env()`, which deliberately keeps the
    credential carrier intact (#2340 fix_delta P0-1 / #2299 / PR #2303).
    Sharing the dependency-removal sanitizer instead left the relationship
    route running credential-starved relative to the context that gates
    it, and could silently strip an inherited GH_CONFIG_DIR even when
    GH_TOKEN/GITHUB_TOKEN were absent.

    This function therefore reuses the same "noise vs. credential" split
    `_build_metadata_sanitized_env()` already applies: it strips only
    execution/log-hygiene noise (`_METADATA_ENV_NOISE_STRIP_KEYS` --
    GH_HOST/GH_REPO/GH_DEBUG/DEBUG/editor-browser/PYTHONPATH/PYTHONHOME)
    and leaves whatever GH_TOKEN / GITHUB_TOKEN / GH_CONFIG_DIR is already
    present in the invocation-scoped environment untouched -- never
    converting between carriers and never synthesizing a carrier that was
    not already present. No new credential broker or generic auth
    abstraction is introduced; this is a direct reuse of the existing
    preflight/readback-safe builder.
    """
    return _build_metadata_sanitized_env()


def _relationship_split_repo(repo: str) -> tuple[str, str] | None:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return None
    return owner, name


def _fetch_self_and_parent(
    number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[dict | None, str]:
    """Single-node readback of {id, number, state, parent}. parent is None
    when the issue currently has no native parent."""
    split = _relationship_split_repo(repo)
    if split is None:
        return None, "repo_slug_malformed"
    owner, name = split
    data, err = _graphql_call(
        gh_bin,
        env,
        _RELATIONSHIP_SELF_AND_PARENT_QUERY,
        {"owner": owner, "name": name, "number": number},
    )
    if err:
        return None, err
    repository = data.get("repository")
    if not isinstance(repository, dict):
        return None, "graphql_response_missing_repository"
    issue = repository.get("issue")
    if not isinstance(issue, dict):
        return None, "graphql_response_missing_issue"
    self_id = issue.get("id")
    self_number = issue.get("number")
    self_state = issue.get("state")
    if not isinstance(self_id, str) or not self_id:
        return None, "graphql_response_self_id_invalid"
    if self_number != number:
        return None, "graphql_response_self_number_mismatch"
    if self_state not in ("OPEN", "CLOSED"):
        return None, "graphql_response_self_state_invalid"
    parent_payload = issue.get("parent")
    parent_info = None
    if isinstance(parent_payload, dict):
        p_id = parent_payload.get("id")
        p_number = parent_payload.get("number")
        p_state = parent_payload.get("state")
        if not isinstance(p_id, str) or not p_id:
            return None, "graphql_response_parent_id_invalid"
        if type(p_number) is not int or p_number <= 0:
            return None, "graphql_response_parent_number_invalid"
        if p_state not in ("OPEN", "CLOSED"):
            return None, "graphql_response_parent_state_invalid"
        parent_info = {"id": p_id, "number": p_number, "state": p_state}
    return {"id": self_id, "number": self_number, "state": self_state, "parent": parent_info}, ""


def _fetch_relationship_field_all_pages(
    number: int, repo: str, gh_bin: str, env: dict[str, str], field: str
) -> tuple[dict | None, str]:
    """Exhaustive cursor-paginated readback of Issue.blockedBy or Issue.blocking.

    Fail-closed on the same class of anomalies as
    _fetch_blocked_by_all_pages: GraphQL errors, missing/malformed response
    shape, cross-page self-identity drift, non-repo nodes, duplicate node
    ids/numbers across pages, a non-progressing cursor, and the total node
    count exceeding ISSUE_RELATIONSHIP_UPDATE_MAX_TOTAL_NODES.
    """
    split = _relationship_split_repo(repo)
    if split is None:
        return None, "repo_slug_malformed"
    owner, name = split
    query = _RELATIONSHIP_FIELD_QUERY_TEMPLATE.replace("%FIELD%", field)

    nodes: list[dict] = []
    seen_numbers: set[int] = set()
    seen_ids: set[str] = set()
    cursor = None
    page_count = 0
    self_id = None
    self_number = None

    while True:
        variables = {"owner": owner, "name": name, "number": number, "cursor": cursor}
        data, err = _graphql_call(gh_bin, env, query, variables)
        if err:
            return None, err
        repository = data.get("repository")
        if not isinstance(repository, dict):
            return None, "graphql_response_missing_repository"
        issue = repository.get("issue")
        if not isinstance(issue, dict):
            return None, "graphql_response_missing_issue"
        if self_id is None:
            self_id = issue.get("id")
            self_number = issue.get("number")
            if not isinstance(self_id, str) or not self_id:
                return None, "graphql_response_self_id_invalid"
            if self_number != number:
                return None, "graphql_response_self_number_mismatch"
        elif issue.get("id") != self_id or issue.get("number") != self_number:
            return None, "graphql_response_self_identity_drift_mid_pagination"

        field_payload = issue.get(field)
        if not isinstance(field_payload, dict):
            return None, f"graphql_response_missing_{field}"
        page_info = field_payload.get("pageInfo")
        if not isinstance(page_info, dict):
            return None, "graphql_response_missing_page_info"
        page_nodes = field_payload.get("nodes")
        if not isinstance(page_nodes, list):
            return None, "graphql_response_missing_nodes"

        page_count += 1
        if page_count > _RELATIONSHIP_MAX_PAGES:
            return None, "graphql_pagination_runaway"

        for node in page_nodes:
            if not isinstance(node, dict):
                return None, "graphql_response_node_not_object"
            node_id = node.get("id")
            node_number = node.get("number")
            node_state = node.get("state")
            node_repo = (node.get("repository") or {}).get("nameWithOwner")
            if not isinstance(node_id, str) or not node_id:
                return None, "graphql_response_node_id_invalid"
            if type(node_number) is not int or node_number <= 0:
                return None, "graphql_response_node_number_invalid"
            if node_state not in ("OPEN", "CLOSED"):
                return None, f"graphql_response_node_state_invalid: {node_state!r}"
            if node_repo != repo:
                return None, f"graphql_response_node_repo_mismatch: {node_repo!r}"
            if node_number in seen_numbers or node_id in seen_ids:
                return None, "graphql_response_duplicate_node_across_pages"
            seen_numbers.add(node_number)
            seen_ids.add(node_id)
            nodes.append({"id": node_id, "number": node_number, "state": node_state})
            if len(nodes) > ISSUE_RELATIONSHIP_UPDATE_MAX_TOTAL_NODES:
                return None, "graphql_response_size_cap_exceeded"

        has_next = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next, bool):
            return None, "graphql_response_has_next_page_not_bool"
        if has_next:
            if not isinstance(end_cursor, str) or not end_cursor or end_cursor == cursor:
                return None, "graphql_response_cursor_invalid_or_not_progressing"
            cursor = end_cursor
            continue
        break

    return {
        "self_id": self_id,
        "self_number": self_number,
        "nodes": nodes,
        "page_count": page_count,
    }, ""


def _lookup_relationship_issue_node(
    number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[dict | None, str]:
    """Resolve {id, number, state} for an arbitrary issue number in `repo`.

    Used to resolve node ids for parent/blocked_by/blocking mutation targets
    that are not the transaction subject itself.
    """
    split = _relationship_split_repo(repo)
    if split is None:
        return None, "repo_slug_malformed"
    owner, name = split
    data, err = _graphql_call(
        gh_bin, env, _ISSUE_NODE_LOOKUP_QUERY, {"owner": owner, "name": name, "number": number}
    )
    if err:
        return None, err
    repository = data.get("repository")
    if not isinstance(repository, dict):
        return None, "graphql_response_missing_repository"
    issue = repository.get("issue")
    if not isinstance(issue, dict):
        return None, "graphql_response_missing_issue"
    node_id = issue.get("id")
    node_number = issue.get("number")
    node_state = issue.get("state")
    if not isinstance(node_id, str) or not node_id:
        return None, "graphql_response_node_id_invalid"
    if node_number != number:
        return None, "graphql_response_node_number_mismatch"
    if node_state not in ("OPEN", "CLOSED"):
        return None, "graphql_response_node_state_invalid"
    return {"id": node_id, "number": node_number, "state": node_state}, ""


def _check_relationship_ancestor_cycle(
    candidate_parent_number: int, self_number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> str:
    """Walk candidate_parent_number's ancestor chain (bounded) looking for
    self_number or a repeated node (existing remote cycle). Returns "" if no
    cycle is found within the bound, else a descriptive error code. AC12."""
    seen: set[int] = set()
    current: int | None = candidate_parent_number
    hops = 0
    while current is not None:
        if current == self_number:
            return "ancestor_cycle_detected"
        if current in seen:
            return "ancestor_cycle_detected_in_existing_remote_graph"
        seen.add(current)
        hops += 1
        if hops > _RELATIONSHIP_MAX_ANCESTOR_WALK:
            return "ancestor_walk_exceeded_bound"
        info, err = _fetch_self_and_parent(current, repo, gh_bin, env)
        if err:
            return f"ancestor_walk_error: {err}"
        parent = info.get("parent")
        current = parent["number"] if parent else None
    return ""


def _execute_relationship_operation(
    op: dict,
    self_number: int,
    self_node_id: str,
    repo: str,
    gh_bin: str,
    env: dict[str, str],
    idempotency_key: str,
) -> tuple[bool, str]:
    """Execute one fixed relationship operation via a single fixed-argv
    GraphQL mutation. No automatic retry. Returns (ok, error)."""
    kind = op["kind"]
    target = op["target"]
    target_info, err = _lookup_relationship_issue_node(target, repo, gh_bin, env)
    if err:
        return False, err
    client_mutation_id = f"{idempotency_key}:{kind}:{target}"

    if kind == "set_parent":
        data, err = _graphql_call(
            gh_bin,
            env,
            _ADD_SUB_ISSUE_MUTATION,
            {
                "issueId": target_info["id"],
                "subIssueId": self_node_id,
                "clientMutationId": client_mutation_id,
            },
        )
        if err:
            return False, err
        payload = data.get("addSubIssue") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return False, "mutation_response_missing_add_sub_issue_payload"
        sub_issue = payload.get("subIssue") or {}
        parent_issue = payload.get("issue") or {}
        if sub_issue.get("number") != self_number:
            return False, "mutation_response_sub_issue_identity_mismatch"
        if parent_issue.get("number") != target:
            return False, "mutation_response_parent_identity_mismatch"
        return True, ""

    if kind == "remove_parent":
        data, err = _graphql_call(
            gh_bin,
            env,
            _REMOVE_SUB_ISSUE_MUTATION,
            {
                "issueId": target_info["id"],
                "subIssueId": self_node_id,
                "clientMutationId": client_mutation_id,
            },
        )
        if err:
            return False, err
        payload = data.get("removeSubIssue") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return False, "mutation_response_missing_remove_sub_issue_payload"
        return True, ""

    if kind == "add_blocked_by":
        data, err = _graphql_call(
            gh_bin,
            env,
            _RELATIONSHIP_ADD_BLOCKED_BY_MUTATION,
            {
                "issueId": self_node_id,
                "blockingIssueId": target_info["id"],
                "clientMutationId": client_mutation_id,
            },
        )
        if err:
            return False, err
        payload = data.get("addBlockedBy") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return False, "mutation_response_missing_add_blocked_by_payload"
        return True, ""

    if kind == "remove_blocked_by":
        data, err = _graphql_call(
            gh_bin,
            env,
            _RELATIONSHIP_REMOVE_BLOCKED_BY_MUTATION,
            {
                "issueId": self_node_id,
                "blockingIssueId": target_info["id"],
                "clientMutationId": client_mutation_id,
            },
        )
        if err:
            return False, err
        payload = data.get("removeBlockedBy") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return False, "mutation_response_missing_remove_blocked_by_payload"
        return True, ""

    if kind == "add_blocking":
        # self (T) blocks target (X): AddBlockedByInput{issueId: X, blockingIssueId: T}
        data, err = _graphql_call(
            gh_bin,
            env,
            _RELATIONSHIP_ADD_BLOCKED_BY_MUTATION,
            {
                "issueId": target_info["id"],
                "blockingIssueId": self_node_id,
                "clientMutationId": client_mutation_id,
            },
        )
        if err:
            return False, err
        payload = data.get("addBlockedBy") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return False, "mutation_response_missing_add_blocking_payload"
        return True, ""

    if kind == "remove_blocking":
        data, err = _graphql_call(
            gh_bin,
            env,
            _RELATIONSHIP_REMOVE_BLOCKED_BY_MUTATION,
            {
                "issueId": target_info["id"],
                "blockingIssueId": self_node_id,
                "clientMutationId": client_mutation_id,
            },
        )
        if err:
            return False, err
        payload = data.get("removeBlockedBy") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return False, "mutation_response_missing_remove_blocking_payload"
        return True, ""

    return False, f"unknown_operation_kind: {kind}"


def _run_issue_relationship_update(args, input_data, gh_bin, _fail, _ok) -> int:
    # Defense in depth: re-validate the same closed-key/graph-invariant
    # contract the caller's own construction is expected to satisfy.
    field_err = validate_issue_relationship_update_input(input_data, args.issue_number, args.repo)
    if field_err:
        return _fail(field_err, status="precondition_rejected", extra={"mutation_attempted": False})

    if args.dry_run:
        return _ok({"status": "dry_run_ok", "mutation_attempted": False})

    env = _relationship_gh_env()

    login, permission, actor_err = _fetch_issue_dependency_remove_actor(gh_bin, env, args.repo)
    if actor_err:
        return _fail(
            actor_err, status="transport_or_schema_error", extra={"mutation_attempted": False}
        )
    if permission not in _RELATIONSHIP_TRUSTED_PERMISSIONS:
        return _fail(
            f"credential_actor_not_authorized: login={login!r} permission={permission!r}",
            status="precondition_rejected",
            extra={"mutation_attempted": False},
        )

    self_info, err = _fetch_self_and_parent(args.issue_number, args.repo, gh_bin, env)
    if err:
        return _fail(err, status="transport_or_schema_error", extra={"mutation_attempted": False})
    blocked_by_state, err = _fetch_relationship_field_all_pages(
        args.issue_number, args.repo, gh_bin, env, "blockedBy"
    )
    if err:
        return _fail(err, status="transport_or_schema_error", extra={"mutation_attempted": False})
    blocking_state, err = _fetch_relationship_field_all_pages(
        args.issue_number, args.repo, gh_bin, env, "blocking"
    )
    if err:
        return _fail(err, status="transport_or_schema_error", extra={"mutation_attempted": False})

    current_parent_number = self_info["parent"]["number"] if self_info["parent"] else None
    current_blocked_by = sorted(n["number"] for n in blocked_by_state["nodes"])
    current_blocking = sorted(n["number"] for n in blocking_state["nodes"])
    before_snapshot = {
        "parent": current_parent_number,
        "blocked_by": current_blocked_by,
        "blocking": current_blocking,
    }

    expected_before = input_data["expected_before"]
    expected_before_normalized = {
        "parent": expected_before.get("parent"),
        "blocked_by": sorted(expected_before.get("blocked_by", [])),
        "blocking": sorted(expected_before.get("blocking", [])),
    }
    # AC11: destructive remove_* / parent-remove must never execute against
    # drifted live state -- this comparison is the sole gate, and it always
    # runs (not only when a destructive operation is requested), since any
    # add/remove computation depends on knowing the true current set.
    if expected_before_normalized != before_snapshot:
        return _fail(
            "precondition_expected_before_drift",
            status="precondition_rejected",
            extra={"mutation_attempted": False, "before": before_snapshot},
        )

    parent_action = input_data["parent"]
    add_bb = input_data["add_blocked_by"]
    rm_bb = input_data["remove_blocked_by"]
    add_bl = input_data["add_blocking"]
    rm_bl = input_data["remove_blocking"]

    desired_parent_number = current_parent_number
    if parent_action["action"] == "set":
        desired_parent_number = parent_action["issue_number"]
    elif parent_action["action"] == "remove":
        desired_parent_number = None

    desired_blocked_by = sorted((set(current_blocked_by) | set(add_bb)) - set(rm_bb))
    desired_blocking = sorted((set(current_blocking) | set(add_bl)) - set(rm_bl))
    desired_snapshot = {
        "parent": desired_parent_number,
        "blocked_by": desired_blocked_by,
        "blocking": desired_blocking,
    }

    if parent_action["action"] == "set" and parent_action["issue_number"] != current_parent_number:
        cycle_err = _check_relationship_ancestor_cycle(
            parent_action["issue_number"], args.issue_number, args.repo, gh_bin, env
        )
        if cycle_err:
            return _fail(
                cycle_err,
                status="precondition_rejected",
                extra={
                    "mutation_attempted": False,
                    "before": before_snapshot,
                    "desired": desired_snapshot,
                },
            )

    parent_noop = (
        parent_action["action"] == "unchanged" or desired_parent_number == current_parent_number
    )
    # PR #1897 P1-2: idempotent no-op is decided by the *effective* diff
    # (current == desired), not by whether the caller's raw add/remove
    # lists happen to be empty. A caller can request add_blocked_by for a
    # target already present, or remove_blocked_by for a target that is not
    # present -- both are redundant no-ops for that entry, and the AC8
    # contract ("current == desired native state produces zero mutation
    # calls") must still hold even when the raw request lists are non-empty.
    if parent_noop and desired_snapshot == before_snapshot:
        return _ok(
            {
                "status": "no_op",
                "mutation_attempted": False,
                "before": before_snapshot,
                "desired": desired_snapshot,
                "after": before_snapshot,
                "completed_operations": [],
                "pending_operations": [],
                "actor_login": login,
                "actor_permission": permission,
            }
        )

    # Operations are built from the *effective* diff (desired vs. current),
    # never from the raw caller-declared add/remove sets, so a redundant
    # add-of-an-already-present-target or remove-of-an-absent-target never
    # produces a GraphQL mutation call.
    effective_remove_bb = sorted(set(current_blocked_by) - set(desired_blocked_by))
    effective_add_bb = sorted(set(desired_blocked_by) - set(current_blocked_by))
    effective_remove_bl = sorted(set(current_blocking) - set(desired_blocking))
    effective_add_bl = sorted(set(desired_blocking) - set(current_blocking))

    write_root = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    )
    pre_mutation_changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if pre_mutation_changed:
        return _fail(
            "precondition_tracked_changes_detected",
            [f"changed: {f}" for f in pre_mutation_changed[:20]],
            status="precondition_rejected",
            extra={
                "mutation_attempted": False,
                "before": before_snapshot,
                "desired": desired_snapshot,
            },
        )

    # Issue #2163: pre-mutation filesystem metadata snapshot captured at this
    # same point (immediately after the precondition above passes and before
    # any GraphQL mutation operation is executed), threaded into the
    # postcondition check below. Named distinctly from `before_snapshot`
    # (the native parent/blockedBy/blocking relationship state) to avoid
    # confusion between the two unrelated "before" concepts.
    pre_mutation_fs_snapshot, pre_mutation_fs_snapshot_err = _capture_pre_mutation_snapshot(
        PROJECT_ROOT, args.issue_number, write_root
    )
    if pre_mutation_fs_snapshot_err is not None:
        # Issue #1667 review fix_delta P1 (via #2163): mirrors the sibling
        # precondition_tracked_changes_detected classification above -- this
        # command id's closed result-status set never includes "failed".
        return _fail(
            f"pre_mutation_snapshot_capture_failed: {pre_mutation_fs_snapshot_err}",
            status="precondition_rejected",
            extra={
                "mutation_attempted": False,
                "before": before_snapshot,
                "desired": desired_snapshot,
            },
        )

    operations: list[dict] = []
    if not parent_noop:
        if parent_action["action"] == "remove" and current_parent_number is not None:
            operations.append({"kind": "remove_parent", "target": current_parent_number})
        elif parent_action["action"] == "set":
            # PR #1897 P1-3: addSubIssue(replaceParent: true) already atomically
            # replaces any existing parent in a single mutation -- issuing a
            # separate remove_parent first only creates an unnecessary
            # window where the issue is briefly parentless if the
            # subsequent set_parent call fails.
            operations.append({"kind": "set_parent", "target": desired_parent_number})
    for n in effective_remove_bb:
        operations.append({"kind": "remove_blocked_by", "target": n})
    for n in effective_add_bb:
        operations.append({"kind": "add_blocked_by", "target": n})
    for n in effective_remove_bl:
        operations.append({"kind": "remove_blocking", "target": n})
    for n in effective_add_bl:
        operations.append({"kind": "add_blocking", "target": n})

    all_operation_labels = [f"{op['kind']}:{op['target']}" for op in operations]
    completed_operations: list[str] = []
    mutation_attempted = False
    op_error: str | None = None

    for op in operations:
        mutation_attempted = True
        op_ok, op_err = _execute_relationship_operation(
            op,
            args.issue_number,
            self_info["id"],
            args.repo,
            gh_bin,
            env,
            input_data["idempotency_key"],
        )
        if not op_ok:
            op_error = f"{op['kind']}:{op['target']}:{op_err}"
            break
        completed_operations.append(f"{op['kind']}:{op['target']}")

    # A failed-or-never-attempted operation is "pending" -- it is always the
    # ordered suffix of all_operation_labels following what actually
    # completed (AC9).
    pending_operations = all_operation_labels[len(completed_operations) :]

    # Always attempt a full post-readback, even after a partial failure, so
    # the caller can classify observed vs. desired state (AC9).
    post_self_info, post_self_err = _fetch_self_and_parent(
        args.issue_number, args.repo, gh_bin, env
    )
    post_bb = post_bb_err = None
    post_bl = post_bl_err = None
    if not post_self_err:
        post_bb, post_bb_err = _fetch_relationship_field_all_pages(
            args.issue_number, args.repo, gh_bin, env, "blockedBy"
        )
    if not post_self_err and not post_bb_err:
        post_bl, post_bl_err = _fetch_relationship_field_all_pages(
            args.issue_number, args.repo, gh_bin, env, "blocking"
        )

    if post_self_err or post_bb_err or post_bl_err:
        return _fail(
            "post_readback_failed_after_mutation_attempt",
            [e for e in (post_self_err, post_bb_err, post_bl_err) if e],
            status="transport_or_schema_error",
            extra={
                "mutation_attempted": mutation_attempted,
                "before": before_snapshot,
                "desired": desired_snapshot,
                "completed_operations": completed_operations,
                "pending_operations": pending_operations,
            },
        )

    after_parent_number = post_self_info["parent"]["number"] if post_self_info["parent"] else None
    after_snapshot = {
        "parent": after_parent_number,
        "blocked_by": sorted(n["number"] for n in post_bb["nodes"]),
        "blocking": sorted(n["number"] for n in post_bl["nodes"]),
    }

    if op_error is not None:
        return _fail(
            op_error,
            status="partial",
            extra={
                "mutation_attempted": True,
                "before": before_snapshot,
                "desired": desired_snapshot,
                "after": after_snapshot,
                "completed_operations": completed_operations,
                "pending_operations": pending_operations,
            },
        )

    if after_snapshot != desired_snapshot:
        return _fail(
            "postcondition_final_state_mismatch",
            status="postcondition_rejected",
            extra={
                "mutation_attempted": True,
                "before": before_snapshot,
                "desired": desired_snapshot,
                "after": after_snapshot,
                "completed_operations": completed_operations,
                "pending_operations": pending_operations,
            },
        )

    changed = _check_no_tracked_changes(
        PROJECT_ROOT, args.issue_number, write_root, pre_mutation_fs_snapshot
    )
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="postcondition_rejected",
            extra={
                "mutation_attempted": True,
                "before": before_snapshot,
                "desired": desired_snapshot,
                "after": after_snapshot,
                "completed_operations": completed_operations,
                "pending_operations": pending_operations,
            },
        )

    return _ok(
        {
            "status": "applied",
            "mutation_attempted": True,
            "before": before_snapshot,
            "desired": desired_snapshot,
            "after": after_snapshot,
            "completed_operations": completed_operations,
            "pending_operations": pending_operations,
            "actor_login": login,
            "actor_permission": permission,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
