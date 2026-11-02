#!/usr/bin/env python3
"""
contract_review_result_parser.py

Issue コメントから valid CONTRACT_REVIEW_RESULT_V1 を解析する共有 parser。
#66 との統合を見越した canonical entry point。

Integration note (#66):
  Issue #66 で将来的に contract result の canonical storage が変更された場合、
  このモジュールを更新することで downstream consumers (run_contract_review_once.py,
  ensure_contract_snapshot.py 等) への影響を最小化する。
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Optional


# ---------------------------------------------------------------------------
# GitHub comment fetching
# ---------------------------------------------------------------------------


def fetch_issue_comments(
    issue_number: int, repo: str, timeout: int = 20
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch all issue comments via gh CLI with pagination.
    Returns (comments_list, error_code_or_None).

    #1475 (fix_delta P1 item 2): the jq projection now also requests
    user.id / user.type so the trusted-publisher check can bind to the full
    GitHub identity tuple (id, login, type, association), not login/
    association alone.

    #1475 (fix_delta P2 item 4): fail-closed NDJSON handling. A single
    malformed line makes the whole fetch untrustworthy for authoritative
    go/blocked precedence -- a truncated/corrupted stream could silently
    drop the newest trusted comment and make a stale one look authoritative.
    Any json.JSONDecodeError on a non-empty line aborts the fetch with
    comments_fetch_incomplete instead of silently continuing with a partial
    comment list.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
                "--jq",
                '.[] | {id, html_url, created_at, updated_at, body, '
                'author: .user.login, author_id: .user.id, '
                'author_type: .user.type, author_association}',
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "not authenticated" in stderr or "authentication failed" in stderr:
                return [], "gh_auth_failed"
            if "not found" in stderr or "could not resolve" in stderr:
                return [], "gh_not_found"
            return [], "gh_other_error"
        # --jq with .[] produces one JSON object per line (NDJSON)
        comments: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                comments.append(json.loads(line))
            except json.JSONDecodeError:
                # Fail-closed (#1475 P2 item 4): a partially-decoded comment
                # stream must never be treated as a complete evidence set.
                return [], "comments_fetch_incomplete"
        return comments, None
    except subprocess.TimeoutExpired:
        return [], "gh_timeout"
    except Exception:
        return [], "gh_other_error"


# ---------------------------------------------------------------------------
# YAML block extraction
# ---------------------------------------------------------------------------

_FENCED_YAML_RE = re.compile(
    r"```ya?ml[ \t]*\n(.*?)```",
    re.DOTALL,
)

_CONTRACT_REVIEW_MARKER = "CONTRACT_REVIEW_RESULT_V1"
_MAX_JSON_FLOW_COLLECTION_CHARS = 16_384
_MAX_JSON_FLOW_COLLECTION_DEPTH = 32


class SimpleYamlParseError(ValueError):
    """Raised when the restricted fallback cannot parse a JSON collection."""


def _extract_yaml_blocks(body: str) -> list[str]:
    """Extract all fenced yaml/yml block contents from a comment body."""
    return [m.group(1) for m in _FENCED_YAML_RE.finditer(body)]


def _reject_nonstandard_json_constant(value: str) -> None:
    """Reject Python's non-standard JSON numeric constants."""
    raise SimpleYamlParseError(f"nonstandard_json_constant:{value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object only when each key appears exactly once."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SimpleYamlParseError("duplicate_json_object_key")
        result[key] = value
    return result


def _json_flow_collection_depth(value: str) -> int:
    """Return flow nesting depth without treating brackets in strings as syntax."""
    depth = 0
    maximum = 0
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            maximum = max(maximum, depth)
        elif char in "}]":
            depth -= 1
    return maximum


def _parse_bounded_strict_json_collection(value: str) -> Any:
    """Parse one producer-designated JSON collection without weakening bounds."""
    if len(value) > _MAX_JSON_FLOW_COLLECTION_CHARS:
        raise SimpleYamlParseError("json_flow_collection_too_large")
    if _json_flow_collection_depth(value) > _MAX_JSON_FLOW_COLLECTION_DEPTH:
        raise SimpleYamlParseError("json_flow_collection_too_deep")
    try:
        return json.loads(
            value,
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except SimpleYamlParseError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SimpleYamlParseError("invalid_json_flow_collection") from exc


def _parse_fallback_scalar(value: str) -> Any:
    """Parse bounded, strict JSON flow collections in an otherwise scalar fallback."""
    if value.startswith(("{", "[")):
        return _parse_bounded_strict_json_collection(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        # YAML single-quoted scalars encode apostrophes by doubling them.
        return value[1:-1].replace("''", "'") if value.startswith("'") else value[1:-1]
    return value


_SINGLE_QUOTED_SCALAR_WHOLE_RE = re.compile(r"^'(?P<payload>(?:[^'\r\n]|'')*)'$")

_EXPECTED_CONTRACT_FINGERPRINT_KEY = "expected_contract_fingerprint"


def _single_quoted_scalar_payload(value: str) -> Optional[str]:
    """Decode ``value`` iff it is exactly one single-quoted YAML scalar
    occupying the whole value position, else return ``None``.

    YAML represents an apostrophe in a single-quoted scalar as ``''``;
    convert that escape only after matching the complete, one-line scalar
    form so a partial/embedded match can never be mistaken for canonical
    transport.
    """
    match = _SINGLE_QUOTED_SCALAR_WHOLE_RE.match(value.strip())
    if match is None:
        return None
    return match.group("payload").replace("''", "'")


def _compose_canonical_fingerprint_scalar(composed: Any) -> Optional[str]:
    """Resolve the exact-node-path canonical fingerprint scalar via PyYAML compose().

    Unlike a regex over the whole raw YAML block, this walks the composed
    node tree to the exact node path
    ``CONTRACT_REVIEW_RESULT_V1.expected_contract_fingerprint`` so that a
    same-named key elsewhere in the document (a sibling top-level key, text
    inside a block scalar, etc.) can never be mistaken for the authoritative
    field. Returns ``None`` when the field is absent or not a single-quoted
    scalar at that exact path. Raises ``SimpleYamlParseError`` when the key
    (or the ``CONTRACT_REVIEW_RESULT_V1`` marker itself) is duplicated in the
    document, since duplicate keys make the authoritative value ambiguous.
    """
    import yaml  # noqa: F401 -- available in project venv (PyYAML)

    if composed is None or not isinstance(composed, yaml.MappingNode):
        return None

    marker_value_node = None
    marker_count = 0
    for key_node, value_node in composed.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == _CONTRACT_REVIEW_MARKER:
            marker_count += 1
            marker_value_node = value_node
    if marker_count == 0:
        return None
    if marker_count > 1:
        raise SimpleYamlParseError("duplicate_contract_review_result_marker_key")
    if not isinstance(marker_value_node, yaml.MappingNode):
        return None

    fingerprint_node = None
    fingerprint_count = 0
    for key_node, value_node in marker_value_node.value:
        if (
            isinstance(key_node, yaml.ScalarNode)
            and key_node.value == _EXPECTED_CONTRACT_FINGERPRINT_KEY
        ):
            fingerprint_count += 1
            fingerprint_node = value_node
    if fingerprint_count == 0:
        return None
    if fingerprint_count > 1:
        raise SimpleYamlParseError("duplicate_contract_fingerprint_key")
    if not isinstance(fingerprint_node, yaml.ScalarNode) or fingerprint_node.style != "'":
        raise SimpleYamlParseError("noncanonical_contract_fingerprint_transport")
    # yaml.compose() already resolves scalar escapes (including the ``''``
    # apostrophe escape), so fingerprint_node.value is the decoded payload.
    return fingerprint_node.value


def _decode_producer_json_scalars(
    block: dict[str, Any],
    *,
    fingerprint_scalar_resolver: Any,
) -> dict[str, Any]:
    """Restore producer JSON transported inside YAML single-quoted scalars.

    Only the producer's fixed complex fields are decoded.  The same strict
    JSON parser and size/depth caps are used for both PyYAML and fallback
    paths, so quoting does not create a permissive YAML or JSON path.

    ``fingerprint_scalar_resolver`` is a zero-argument callable, bound by the
    caller to the exact node path ``CONTRACT_REVIEW_RESULT_V1.
    expected_contract_fingerprint`` (via PyYAML compose() or the fallback
    state machine), that returns the canonical single-quoted scalar text or
    ``None``, and may raise ``SimpleYamlParseError`` for duplicate keys.
    """
    inner = block.get(_CONTRACT_REVIEW_MARKER)
    if not isinstance(inner, dict):
        return block

    def decode(mapping: dict[str, Any], key: str) -> None:
        value = mapping.get(key)
        if isinstance(value, str) and value.startswith(("{", "[")):
            mapping[key] = _parse_bounded_strict_json_collection(value)

    checks = inner.get("checks")
    # The producer's fingerprint transport is a single-quoted JSON mapping.
    # A YAML flow or nested mapping has the same Python type after PyYAML
    # parsing, so provenance must be checked against the raw transport before
    # it can reach an authority consumer.  Reject instead of merely leaving a
    # noncanonical value non-ready: legacy/provisional GO records may omit the
    # field, but a supplied noncanonical fingerprint must not be surfaced as a
    # candidate with ambiguous authority semantics.
    if _EXPECTED_CONTRACT_FINGERPRINT_KEY in inner:
        raw_fingerprint = fingerprint_scalar_resolver()
        if raw_fingerprint is None:
            raise SimpleYamlParseError("noncanonical_contract_fingerprint_transport")
        fingerprint = _parse_bounded_strict_json_collection(raw_fingerprint)
        if not isinstance(fingerprint, dict):
            raise SimpleYamlParseError("expected_contract_fingerprint_must_be_mapping")
        inner[_EXPECTED_CONTRACT_FINGERPRINT_KEY] = fingerprint
    if not isinstance(checks, dict):
        return block
    try:
        decode(checks, "declared_path_overlap")
    except SimpleYamlParseError:
        # Overlap is advisory only. Keep its raw scalar so malformed or
        # oversized producer telemetry cannot invalidate source-bound fields.
        pass
    vc_preflight = checks.get("vc_preflight")
    if isinstance(vc_preflight, dict):
        decode(vc_preflight, "classifications")
        if not isinstance(vc_preflight.get("classifications"), list):
            raise SimpleYamlParseError("vc_preflight_classifications_must_be_list")
    return block


def _parse_simple_yaml_block(block: str) -> dict[str, Any]:
    """
    Parse a YAML block using yaml.safe_load when available, with a
    minimal key-value fallback for environments where PyYAML is absent.

    Preference: yaml.safe_load (PyYAML) — handles nested structures, quoted
    strings, and all standard YAML scalar types correctly.
    Fallback: the legacy flat + one-level indentation mapping parser. It
    deliberately does not implement general YAML syntax; a scalar value that
    starts with ``{`` or ``[`` is accepted only as bounded, strict JSON.
    """
    try:
        import yaml  # noqa: F401 — available in project venv (PyYAML)
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            try:
                composed = yaml.compose(block)
            except Exception:
                composed = None
            return _decode_producer_json_scalars(
                parsed,
                fingerprint_scalar_resolver=lambda: _compose_canonical_fingerprint_scalar(
                    composed
                ),
            )
        return {}
    except Exception:
        pass

    # --- Minimal fallback parser (used only when yaml is unavailable) ---
    result: dict[str, Any] = {}
    current_key: Optional[str] = None
    inside_checks = False
    current_checks_mapping: Optional[str] = None
    # Track the raw (pre-_parse_fallback_scalar) text of
    # CONTRACT_REVIEW_RESULT_V1.expected_contract_fingerprint at its exact
    # structural position (current_key == marker, indent == 2) so the
    # canonical-scalar check below is bound to that node path instead of a
    # whole-block regex scan, matching the PyYAML compose() path.
    fingerprint_raw: Optional[str] = None
    fingerprint_seen = False
    fingerprint_duplicate = False

    for line in block.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        match = re.match(r'^\s*(\S[^:]*?):\s*(.*)', stripped)
        if indent == 0:
            current_key = None
            inside_checks = False
            current_checks_mapping = None
            if match is None:
                continue
            key = match.group(1).strip()
            value = match.group(2).strip()
            result[key] = _parse_fallback_scalar(value) if value else None
            if not value:
                current_key = key
        elif current_key is not None and match is not None and indent == 2:
            sub_key = match.group(1).strip()
            sub_value = match.group(2).strip()
            if not isinstance(result.get(current_key), dict):
                result[current_key] = {}
            inside_checks = current_key == _CONTRACT_REVIEW_MARKER and sub_key == "checks" and not sub_value
            if (
                current_key == _CONTRACT_REVIEW_MARKER
                and sub_key == _EXPECTED_CONTRACT_FINGERPRINT_KEY
            ):
                if fingerprint_seen:
                    fingerprint_duplicate = True
                fingerprint_seen = True
                fingerprint_raw = sub_value
            result[current_key][sub_key] = (
                {} if inside_checks else _parse_fallback_scalar(sub_value) if sub_value else None
            )
            current_checks_mapping = None
        elif (
            current_key == _CONTRACT_REVIEW_MARKER
            and inside_checks
            and match is not None
            and indent == 4
        ):
            checks = result[current_key].get("checks")
            if not isinstance(checks, dict):
                continue
            sub_key = match.group(1).strip()
            sub_value = match.group(2).strip()
            # This is a producer-compatible exception, not general YAML:
            # ensure_contract_snapshot emits only checks.vc_preflight as a
            # nested mapping; all other checks values are scalar or JSON flow.
            if sub_key == "vc_preflight" and not sub_value:
                checks[sub_key] = {}
                current_checks_mapping = "checks.vc_preflight"
            else:
                checks[sub_key] = _parse_fallback_scalar(sub_value) if sub_value else None
                current_checks_mapping = None
        elif (
            current_key == _CONTRACT_REVIEW_MARKER
            and current_checks_mapping == "checks.vc_preflight"
            and match is not None
            and indent == 6
        ):
            checks = result[current_key].get("checks")
            vc_preflight = checks.get("vc_preflight") if isinstance(checks, dict) else None
            if not isinstance(vc_preflight, dict):
                continue
            sub_key = match.group(1).strip()
            sub_value = match.group(2).strip()
            vc_preflight[sub_key] = (
                _parse_fallback_scalar(sub_value) if sub_value else None
            )

    def _fallback_fingerprint_resolver() -> Optional[str]:
        if fingerprint_duplicate:
            raise SimpleYamlParseError("duplicate_contract_fingerprint_key")
        if not fingerprint_seen or fingerprint_raw is None:
            return None
        return _single_quoted_scalar_payload(fingerprint_raw)

    return _decode_producer_json_scalars(
        result, fingerprint_scalar_resolver=_fallback_fingerprint_resolver
    )


# ---------------------------------------------------------------------------
# Trust policy (GitHub provenance, #1475)
# ---------------------------------------------------------------------------

# Trusted GitHub author_association values. NONE/CONTRIBUTOR/FIRST_TIME_CONTRIBUTOR
# are excluded so that arbitrary outside commenters cannot post an authoritative
# snapshot. Retained for callers that only need the (weaker) association-only
# check as a display/audit signal; it is no longer sufficient on its own to
# authorize a snapshot -- see TRUSTED_CONTRACT_PUBLISHERS below (#1475).
TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)

# #1475 (fix_delta P1 item 2): static allowlist of controlled contract-snapshot
# publishers, keyed by the immutable GitHub user.id. author_association alone
# authorizes any current repo COLLABORATOR/MEMBER, which is too broad for an
# authoritative security-relevant snapshot. Authorization now requires the
# full (user.id, user.login, user.type, author_association) tuple to match a
# single allowlisted entry exactly. login is used for audit display and
# rename-drift detection only -- it is never sufficient by itself, and
# association is never sufficient by itself either.
TRUSTED_CONTRACT_PUBLISHERS: dict[int, dict[str, Any]] = {
    63350259: {
        "expected_login": "squne121",
        "expected_type": "User",
        "allowed_associations": frozenset({"OWNER"}),
    },
}


def is_trusted_snapshot_author(
    author: Optional[str],
    author_association: Optional[str],
    *,
    author_id: Optional[int] = None,
    author_type: Optional[str] = None,
) -> bool:
    """
    Decide whether a GitHub comment's full identity tuple is allowed to
    publish an authoritative CONTRACT_REVIEW_RESULT_V1 snapshot.

    Fail-closed (#1475 P1 item 2): identity authorization requires an exact
    match against TRUSTED_CONTRACT_PUBLISHERS, keyed by author_id (GitHub
    user.id, immutable). author_association alone is never sufficient --
    it no longer authorizes arbitrary repo COLLABORATOR/MEMBER accounts.

    Validation order:
      1. author_id must be present, a non-bool int, and > 0.
      2. author_id must be a key in TRUSTED_CONTRACT_PUBLISHERS.
      3. author (login) must equal the allowlisted expected_login.
      4. author_type must equal the allowlisted expected_type.
      5. author_association must be one of the allowlisted associations.
    All five conditions must hold; any missing/mismatched field is untrusted.
    """
    if author_id is None or isinstance(author_id, bool) or not isinstance(author_id, int):
        return False
    if author_id <= 0:
        return False

    entry = TRUSTED_CONTRACT_PUBLISHERS.get(author_id)
    if entry is None:
        return False

    if not author or author != entry["expected_login"]:
        return False
    if not author_type or author_type != entry["expected_type"]:
        return False
    if not author_association or author_association not in entry["allowed_associations"]:
        return False

    return True


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint (Issue #1537)
# ---------------------------------------------------------------------------

# The 7-item fingerprint schema. Must stay identical to
# pr-review-judge/scripts/allowed_paths_review_gate.py ContractFingerprint --
# both producer (ensure_contract_snapshot.py) and consumer (this parser, the
# reviewer gate) must agree on the same key set for source-bound freshness
# comparison to be meaningful.
_FINGERPRINT_REQUIRED_KEYS = (
    "issue_number",
    "contract_source_kind",
    "contract_source_id",
    "contract_body_sha256",
    "allowed_paths_normalized_sha256",
    "base_ref",
    "base_sha_at_snapshot",
)

# contract_body_sha256 follows the repo-wide `sha256:<64 hex>` convention used
# by body_sha256 elsewhere in this schema (ensure_contract_snapshot.sha256_of).
_PREFIXED_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# allowed_paths_normalized_sha256 must byte-for-byte match
# AllowedPathsGateEvaluator.compute_allowed_paths_hash(), which returns a bare
# hex digest with NO "sha256:" prefix -- do not add one here.
_BARE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FULL_COMMIT_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")

_ISSUE_NUMBER_FROM_URL_RE = re.compile(r"/issues/(\d+)\Z")


def _issue_number_from_url(issue_url: Optional[str]) -> Optional[int]:
    """Extract the trailing issue number from a GitHub issue URL."""
    if not issue_url:
        return None
    m = _ISSUE_NUMBER_FROM_URL_RE.search(issue_url)
    return int(m.group(1)) if m else None


def is_fingerprint_ready_go(
    inner: Any,
    comment_id: Optional[int] = None,
    issue_number: Optional[int] = None,
) -> bool:
    """
    Return whether a parsed CONTRACT_REVIEW_RESULT_V1 `inner` block carries a
    well-formed, source-bound `expected_contract_fingerprint` (Issue #1537).

    Fail-closed: any missing key, wrong type, malformed hash, or mismatch
    between the fingerprint's self-declared contract_source_id / issue_number
    and the actual comment / issue this block was read from makes the result
    NOT fingerprint-ready (False), regardless of `status`. `comment_id` /
    `issue_number` are independent authorities (the real GitHub comment id
    the block was parsed from, and the issue this parse run is scoped to) --
    when supplied they are cross-checked against the fingerprint's own
    self-reported values rather than trusted at face value.
    """
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
    ):
        return False
    if not isinstance(inner, dict):
        return False
    fingerprint = inner.get("expected_contract_fingerprint")
    if not isinstance(fingerprint, dict):
        return False
    if set(fingerprint) != set(_FINGERPRINT_REQUIRED_KEYS):
        return False

    fp_issue_number = fingerprint.get("issue_number")
    if isinstance(fp_issue_number, bool) or not isinstance(fp_issue_number, int):
        return False
    if fp_issue_number <= 0:
        return False
    if fp_issue_number != issue_number:
        return False

    if fingerprint.get("contract_source_kind") != "issue_comment":
        return False

    contract_source_id = fingerprint.get("contract_source_id")
    if not isinstance(contract_source_id, str) or not contract_source_id.isdigit():
        return False
    if str(comment_id) != contract_source_id:
        return False

    contract_body_sha256 = fingerprint.get("contract_body_sha256")
    if not isinstance(contract_body_sha256, str) or not _PREFIXED_SHA256_RE.fullmatch(
        contract_body_sha256
    ):
        return False
    if contract_body_sha256 != inner.get("body_sha256"):
        return False

    allowed_paths_hash = fingerprint.get("allowed_paths_normalized_sha256")
    if not isinstance(allowed_paths_hash, str) or not _BARE_SHA256_RE.fullmatch(
        allowed_paths_hash
    ):
        return False

    base_ref = fingerprint.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref:
        return False

    base_sha_at_snapshot = fingerprint.get("base_sha_at_snapshot")
    if not isinstance(base_sha_at_snapshot, str) or not _FULL_COMMIT_OID_RE.fullmatch(
        base_sha_at_snapshot
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_valid_contract_review_result(
    block: dict,
    expected_issue_url: Optional[str] = None,
) -> bool:
    """
    Validate that a parsed YAML block is a valid CONTRACT_REVIEW_RESULT_V1.

    Required fields:
      - status: go | blocked
      - generated_by: issue-contract-review
      - issue_url: must match expected_issue_url if provided
      - generated_at: non-empty ISO8601-like string
    """
    # Must have CONTRACT_REVIEW_RESULT_V1 as root key
    if _CONTRACT_REVIEW_MARKER not in block:
        return False

    inner = block.get(_CONTRACT_REVIEW_MARKER)
    if not isinstance(inner, dict):
        return False

    # status must be go or blocked
    status = inner.get("status", "")
    if status not in ("go", "blocked"):
        return False

    # generated_by must be issue-contract-review
    if inner.get("generated_by") != "issue-contract-review":
        return False

    # generated_at must be non-empty
    if not inner.get("generated_at"):
        return False

    # issue_url must match if expected
    issue_url = inner.get("issue_url", "")
    if expected_issue_url and issue_url != expected_issue_url:
        return False

    # A fingerprint is optional for legacy/provisional GO records, but when
    # supplied it must already be a mapping.  In particular, a double-encoded
    # JSON scalar is neither the producer's canonical single-quoted JSON
    # transport nor a usable flow mapping.  Do not surface it as a GO result:
    # downstream authority checks distinguish an absent current GO candidate
    # from a candidate whose mapping merely differs from the expected value.
    if (
        "expected_contract_fingerprint" in inner
        and not isinstance(inner["expected_contract_fingerprint"], dict)
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_contract_review_results(
    comments: list[dict],
    expected_issue_url: Optional[str] = None,
) -> list[dict]:
    """
    Parse all comments and return list of valid CONTRACT_REVIEW_RESULT_V1 dicts.

    Each returned item has:
      {
        "comment_id": int,
        "html_url": str,
        "created_at": str,
        "block": {  CONTRACT_REVIEW_RESULT_V1: {...} },
        "inner": { status, generated_by, issue_url, generated_at, ... },
        "status": "go" | "blocked",
      }

    Results are ordered by created_at ascending (comment_id ascending).
    """
    results, _telemetry = parse_contract_review_results_with_telemetry(
        comments, expected_issue_url
    )
    return results


def parse_contract_review_results_with_telemetry(
    comments: list[dict],
    expected_issue_url: Optional[str] = None,
) -> tuple[list[dict], dict[str, int]]:
    """Parse canonical results and report non-authoritative block telemetry.

    Consumers that need invalid/ambiguous-block diagnostics must use this
    helper instead of independently decoding YAML.  The returned result
    records retain the same authority and fingerprint semantics as
    :func:`parse_contract_review_results`.
    """
    results: list[dict] = []
    invalid_contract_blocks_count = 0
    ambiguous_contract_blocks_count = 0

    for comment in comments:
        body = comment.get("body", "") or ""
        if _CONTRACT_REVIEW_MARKER not in body:
            continue

        valid_blocks: list[dict] = []
        blocks = _extract_yaml_blocks(body)
        for raw_block in blocks:
            # Only consider blocks that contain the marker
            if _CONTRACT_REVIEW_MARKER not in raw_block:
                continue
            try:
                parsed = _parse_simple_yaml_block(raw_block)
            except SimpleYamlParseError:
                # A malformed JSON flow collection is never downgraded to a
                # scalar candidate for an authoritative contract result.
                invalid_contract_blocks_count += 1
                continue
            if _is_valid_contract_review_result(parsed, expected_issue_url):
                inner = parsed[_CONTRACT_REVIEW_MARKER]
                if not isinstance(inner, dict):
                    continue
                author = comment.get("author")
                author_association = comment.get("author_association")
                author_id = comment.get("author_id")
                author_type = comment.get("author_type")
                valid_blocks.append(
                    {
                        "comment_id": comment.get("id"),
                        "html_url": comment.get("html_url", ""),
                        "created_at": comment.get("created_at", ""),
                        "block": parsed,
                        "inner": inner,
                        "status": inner.get("status", ""),
                        "author": author,
                        "author_association": author_association,
                        "author_id": author_id,
                        "author_type": author_type,
                        "is_trusted_author": is_trusted_snapshot_author(
                            author,
                            author_association,
                            author_id=author_id,
                            author_type=author_type,
                        ),
                        "is_fingerprint_ready": is_fingerprint_ready_go(
                            inner,
                            comment.get("id"),
                            _issue_number_from_url(expected_issue_url),
                        ),
                    }
                )
            else:
                invalid_contract_blocks_count += 1

        if len(valid_blocks) > 1:
            ambiguous_contract_blocks_count += 1
        if valid_blocks:
            # Only take the first valid block per comment, preserving the
            # pre-existing consumer contract while surfacing ambiguity.
            results.append(valid_blocks[0])

    results.sort(
        key=lambda item: (str(item.get("created_at") or ""), int(item.get("comment_id") or 0))
    )
    return results, {
        "invalid_contract_blocks_count": invalid_contract_blocks_count,
        "ambiguous_contract_blocks_count": ambiguous_contract_blocks_count,
    }


def filter_authoritative_results(results: list[dict]) -> list[dict]:
    """
    Return only the subset of parsed results whose author identity passes
    is_trusted_snapshot_author (#1475 P1 item 1).

    This is the single shared authoritative-candidate set: it MUST be applied
    to go/blocked precedence decisions BEFORE choosing the "latest" result,
    not only when selecting a go candidate. Applying trust filtering only to
    find_latest_go (and leaving find_latest_result unfiltered) allows an
    untrusted `status: blocked` comment posted after a trusted `status: go`
    to incorrectly take precedence and halt the workflow (the exact bug this
    function closes).
    """
    return [r for r in results if r.get("is_trusted_author") is True]


def find_latest_go(
    results: list[dict],
    *,
    trusted_only: bool = False,
    fingerprint_ready_only: bool = False,
) -> Optional[dict]:
    """
    Return the latest (by created_at desc, comment_id desc) valid
    CONTRACT_REVIEW_RESULT_V1 with status: go.
    Returns None if no go result found.

    trusted_only (#1475): when True, results whose is_trusted_author is not
    True are excluded from candidacy. A schema-valid but untrusted
    `status: go` is never returned as authoritative when trusted_only=True.

    fingerprint_ready_only (#1537): when True, results whose
    is_fingerprint_ready is not True are excluded from candidacy. A
    schema-valid, trusted `status: go` that lacks a well-formed source-bound
    expected_contract_fingerprint is never returned as loop-consumable when
    fingerprint_ready_only=True -- callers must re-materialize instead.
    """
    go_results = [r for r in results if r["status"] == "go"]
    if trusted_only:
        go_results = filter_authoritative_results(go_results)
    if fingerprint_ready_only:
        go_results = [r for r in go_results if r.get("is_fingerprint_ready") is True]
    if not go_results:
        return None
    # Sort by created_at desc, then comment_id desc
    go_results.sort(key=lambda r: (r.get("created_at", ""), r.get("comment_id", 0)), reverse=True)
    return go_results[0]


def find_latest_authoritative_go(results: list[dict]) -> Optional[dict]:
    """Return the single loop-consumable GO candidate.

    Trusted-but-provisional/orphan comments are intentionally excluded here.
    All authoritative consumers must use this predicate.
    """
    return find_latest_go(
        results, trusted_only=True, fingerprint_ready_only=True
    )


def find_latest_result(
    results: list[dict],
    *,
    trusted_only: bool = False,
) -> Optional[dict]:
    """
    Return the latest (by created_at desc, comment_id desc) valid
    CONTRACT_REVIEW_RESULT_V1 regardless of status.

    trusted_only (#1475 P1 item 1): when True, only authoritative
    (trusted-author) results are considered before selecting the latest
    entry. Every consumer that uses find_latest_result to decide go/blocked
    precedence MUST pass trusted_only=True, or an untrusted outsider comment
    can silently pre-empt a trusted go/blocked snapshot regardless of which
    status it carries.
    """
    candidates = filter_authoritative_results(results) if trusted_only else results
    if not candidates:
        return None
    sorted_results = sorted(
        candidates,
        key=lambda r: (r.get("created_at", ""), r.get("comment_id", 0)),
        reverse=True,
    )
    return sorted_results[0]
