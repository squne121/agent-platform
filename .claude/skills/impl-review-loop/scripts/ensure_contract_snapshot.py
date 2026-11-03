#!/usr/bin/env python3
"""
ensure_contract_snapshot.py

impl-review-loop の missing_contract_go 分岐で呼ばれる orchestration script。
Issue に有効な CONTRACT_REVIEW_RESULT_V1 status: go コメントが存在するか確認し、
存在しない場合は issue-contract-review を自動実行して go コメントを取得・投稿する。

Exit codes (normal producer result) — ensure_contract_snapshot() が返した result の
status から main() が決める終了コード (#3005。実挙動は main() 末尾の分岐が正本):
  0 ok — contract snapshot が確認または materialize できた
  10 blocked_needs_refinement — contract blocked / readiness blocked
  20 human_judgment, dry_run_would_post — 2 つの status が同じ exit 20 を共有する。
      終了コードでは区別できないため、stdout JSON の status で区別する
      (EXIT20_DISAMBIGUATE_BY_STATUS)。human_judgment は分類不能 / ambiguous /
      env error、dry_run_would_post は正常な dry-run 候補生成 (GitHub 投稿なし)
  40 runtime_error — subprocess / network エラー。表にない未知 status もここへ落ちる
  50 stale_or_conflicting_snapshot — atomicity 検証で body_sha256 or updatedAt mismatch
  60 controlled_publisher_binding_failed — #1475: 投稿直後の comment ID readback binding 不一致・欠落

Exit codes (CLI front stage) — result 生成より前に終了する経路 (#3005):
  30 runtime_error — 整数変換後の Issue 番号が 0 の場合のみ
      (--issue-number 0 / --issue 0 のどちらでも同じ)。status=runtime_error の JSON
      (errors: ["--issue-number is required"]) を stdout に出す。
      ensure_contract_snapshot() にも artifact 保存にも到達しない。
      status が invalid_input になることはない (EXIT30_ONLY_ISSUE_NUMBER_ZERO)
  2 argparse_usage_error — 必須引数 (--issue-number) の欠落、または整数型不正
      (例: --issue-number abc)。argparse が stderr に診断を出して終了し、
      stdout は通常空
  0 argparse_help — --help。argparse が stdout にヘルプテキストを出して終了する

終了コードから status を一意に復元することはできない。exit 0 は ok と --help の双方、
exit 40 は runtime_error と未知 status の双方を含み、runtime_error は exit 30 と 40 の
両方に現れる。

stdout: CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 compact JSON のみ。
  ただし例外として argparse の早期終了は JSON を出さない
  (exit 2 は stdout が通常空で診断は stderr、--help は stdout にヘルプテキスト)。
stderr: diagnostic messages のみ (argparse の使用法エラーの診断もここ)

Artifact contract (--artifact-dir、#3005):
  artifact_saved: --artifact-dir 指定で保存に成功した場合に限り、保存先を示す
      artifact_path が stdout の JSON にだけ追加される。保存された artifact
      (contract-snapshot-<N>.json) は stdout の JSON から artifact_path を除いた
      ものと内容が一致する (バイト列一致ではない。ARTIFACT_OMITS_ARTIFACT_PATH)
  artifact_save_failed: 保存に失敗した場合は errors に artifact_write_error: ... が
      追加されるだけで、artifact_path は追加されず、status も終了コードも変わらない。
      status が ok なら保存に失敗しても exit 0 のまま
  artifact_dir_unspecified: --artifact-dir 未指定では保存処理自体がなく、
      ファイルは作られず artifact_path も追加されない
  exit 0 は今回の artifact 保存成功の証明ではない。保存成功は stdout JSON の
  artifact_path と errors で判定する。上記の目印 (EXIT20_DISAMBIGUATE_BY_STATUS 等) は
  検索用であり、テストでも正しさの証明に使わない。

Modes:
  check-only  — 既存 go コメントを確認するのみ。mutation なし (default)
  auto        — go コメントがなければ run_contract_review_once.py を実行
  dry-run     — auto と同じ判定だが GitHub 投稿はしない

--post: GitHub mutation を有効化 (auto mode 以外では無視)

idempotency marker:
  <!-- agent-platform:contract-snapshot issue=<N> body_sha256=sha256:<...> schema=CONTRACT_REVIEW_RESULT_V1 -->

body/comment snapshot atomicity (B2):
  最初に一括取得し body_sha256, issue_updated_at, comments_digest を保存。
  投稿直前に再取得して比較。
  body_sha256 変化 OR updatedAt 変化 OR latest blocked コメント出現 → exit 50。

API error classification (403/429/422 blind retry 禁止):
  not_requested | dry_run_would_post | posted | deduped_existing |
  permission_denied | rate_limited | validation_failed_or_spam | ambiguous_no_retry

Schema key: post_status (not post_result — B4)

status: ok implies contract_snapshot_url is not None (B3).
dry-run / no-post → status: dry_run_would_post (not ok).

Comment posting: gh api REST (B5) for precise HTTP status classification.
V1 comment includes checks summary (B6).

Security scope (#1475 fix_delta P1 item 3 -- explicit, deliberately narrowed
claim rather than an unimplemented receipt schema):
  "Authoritative" means: authored by a GitHub account present in
  contract_review_result_parser.TRUSTED_CONTRACT_PUBLISHERS (an exact
  user.id + user.login + user.type + author_association match), which today
  is the repo OWNER account only. It does NOT mean "identical to the exact
  bytes this specific ensure_contract_snapshot.py process instance posted
  moments ago" for every code path:
    - The publish-time path (POST_STATUS_POSTED) DOES verify the freshly
      posted comment id, issue binding, publisher identity, and comment
      body hash via an independent direct-GET readback
      (verify_controlled_publisher_comment_id_binding).
    - The existing-snapshot-reuse path (source: existing_go, status: ok
      without a POST in this run) does NOT re-run that binding check. It is
      protected instead by the strict identity-tuple allowlist applied to
      every comment considered a candidate (only the allowlisted account
      can ever produce an authoritative go/blocked entry) plus the
      body_sha256 / vc_preflight / product_spec_check freshness checks in
      is_go_current(). It does not protect against the allowlisted account's
      own comment being edited after posting to a body that still hashes to
      a value is_go_current() would accept as current -- that residual risk
      is intentionally out of scope for this Issue and tracked as a
      follow-up (CONTRACT_SNAPSHOT_PUBLISH_RECEIPT_V1, option 1 in the
      Issue #1475 fix_delta) rather than claimed as solved here.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=impl-review-loop, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

_ICR_SCRIPTS_DIR = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
)
_RUN_CONTRACT_REVIEW_ONCE_PY = _ICR_SCRIPTS_DIR / "run_contract_review_once.py"
_CONTRACT_REVIEW_RESULT_PARSER_PY = _ICR_SCRIPTS_DIR / "contract_review_result_parser.py"

# #1537: single source of truth for Allowed Paths normalization / hashing,
# shared with the reviewer's Allowed Paths gate so the fingerprint's
# allowed_paths_normalized_sha256 is byte-for-byte reproducible by both sides.
_PRJ_SCRIPTS_DIR = (
    _REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts"
)
_ALLOWED_PATHS_REVIEW_GATE_PY = _PRJ_SCRIPTS_DIR / "allowed_paths_review_gate.py"
_BASELINE_VC_PREFLIGHT_PY = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
    / "baseline_vc_preflight.py"
)

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402

_DEFAULT_REPO = "squne121/agent-platform"
_DEFAULT_TIMEOUT = 30
# Outer deadline for the bounded contract-review materializer subprocess.
# Per-command VC timeout/default semantics remain owned by
# run_contract_review_once.py and are intentionally not passed through here.
_VC_TIMEOUT = 360
_MAX_CLASSIFICATIONS_TRANSPORT_CHARS = 16_000
_CLASSIFICATIONS_TRANSPORT_SCHEMA = "vc_preflight_classifications_transport/v1"
_CLASSIFICATIONS_TRANSPORT_OMIT_KEYS = frozenset(
    {
        "stdout_head",
        "stderr_head",
        "runner_env_delta",
        "pnpm_gate_evidence",
    }
)

# Idempotency marker template
_IDEMPOTENCY_MARKER_TEMPLATE = (
    "<!-- agent-platform:contract-snapshot issue={issue} "
    "body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
)

# API post status codes (B4: key is post_status throughout)
POST_STATUS_NOT_REQUESTED = "not_requested"
POST_STATUS_DRY_RUN = "dry_run_would_post"
POST_STATUS_POSTED = "posted"
POST_STATUS_DEDUPED = "deduped_existing"
POST_STATUS_PERMISSION_DENIED = "permission_denied"
POST_STATUS_RATE_LIMITED = "rate_limited"
POST_STATUS_VALIDATION_FAILED = "validation_failed_or_spam"
POST_STATUS_AMBIGUOUS = "ambiguous_no_retry"

# Legacy aliases for tests that import old names — mapped to new values
POST_RESULT_NOT_REQUESTED = POST_STATUS_NOT_REQUESTED
POST_RESULT_DRY_RUN = POST_STATUS_DRY_RUN
POST_RESULT_POSTED = POST_STATUS_POSTED
POST_RESULT_DEDUPED = POST_STATUS_DEDUPED
POST_RESULT_PERMISSION_DENIED = POST_STATUS_PERMISSION_DENIED
POST_RESULT_RATE_LIMITED = POST_STATUS_RATE_LIMITED
POST_RESULT_VALIDATION_FAILED = POST_STATUS_VALIDATION_FAILED
POST_RESULT_AMBIGUOUS = POST_STATUS_AMBIGUOUS


# ---------------------------------------------------------------------------
# Import helper for contract_review_result_parser
# ---------------------------------------------------------------------------


def _import_parser_module():
    """Import contract_review_result_parser from ICR scripts dir."""
    spec = importlib.util.spec_from_file_location(
        "contract_review_result_parser",
        _CONTRACT_REVIEW_RESULT_PARSER_PY,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load contract_review_result_parser")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Body + updatedAt snapshot helpers (B2)
# ---------------------------------------------------------------------------


# I-4-2 (#66) transport finding (measured with gh 2.102.0 against live GitHub):
# GitHub stores the comment body unchanged.  The `gh api` CLI sanitizes the JSON it
# *prints*: raw U+0001-0007, U+000E-001F and U+0080-009F, and the JSON escape text
# \u00XX for XX in 01-08, 0c, 0e-1f, 80-9f (any number of preceding backslashes), become
# caret notation.  Tab/LF/VT/FF/CR/BS/DEL, other text and the spelling \u005c all pass.
# The readback this module hashes goes through `gh api`, so a body containing any of
# those would read back different from what was posted (binding_body_hash_mismatch).
# A body must therefore be a fixed point of that sanitizer:
#   * a literal backslash directly before u00XX is spelled \u005c (JSON-equivalent);
#   * a real control character that JSON can only spell as \u00XX / raw is
#     unrepresentable: verbose log values show it as the literal \xNN text, every
#     other (authority-bearing) value is rejected instead of silently changed.
_GH_UNSAFE_CONTROL_RE = re.compile("[\x00-\x07\x0e-\x1f\x80-\x9f\ud800-\udfff]")
_GH_BACKSLASH_BEFORE_CTRL_ESCAPE_RE = re.compile(
    r"(\\\\)(?=u00(?:[01][0-9a-fA-F]|[89][0-9a-fA-F]))|\\.", re.DOTALL
)


# Consumer bounds (contract_review_result_parser): one JSON flow collection is at most
# 16384 chars and 32 deep; GitHub caps a comment body at 65536 chars.
_PARSER_MAX_JSON_CHARS = 16_384
_PARSER_MAX_JSON_DEPTH = 32
_MAX_SNAPSHOT_BODY_CHARS = 60_000


def _worst_case_fingerprint(issue_number: int, body_sha256: str) -> dict[str, Any]:
    """Fingerprint-shaped placeholder at least as large as any real one.

    The pre-POST dry build must bound the *final* body, which embeds the real fingerprint.
    """
    return {
        "issue_number": issue_number,
        "contract_source_kind": "issue_comment",
        "contract_source_id": "9" * 20,
        "contract_body_sha256": body_sha256,
        "allowed_paths_normalized_sha256": "f" * 64,
        "base_ref": "x" * 255,
        "base_sha_at_snapshot": "f" * 64,
    }


def _json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_json_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(v) for v in value), default=0)
    return 0


def _reject_gh_unsafe_control(value: Any) -> None:
    if isinstance(value, str):
        if _GH_UNSAFE_CONTROL_RE.search(value) is not None:
            raise ValueError("snapshot_transport_control_char_unrepresentable")
    elif isinstance(value, list):
        for item in value:
            _reject_gh_unsafe_control(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_gh_unsafe_control(key)
            _reject_gh_unsafe_control(item)


def _json_compact_measure(value: Any) -> str:
    """Compact JSON that survives the `gh api` readback sanitizer byte-for-byte.

    Raises ValueError when ``value`` holds a control character that cannot be
    transported without changing its meaning.
    """
    _reject_gh_unsafe_control(value)
    if _json_depth(value) > _PARSER_MAX_JSON_DEPTH:
        raise ValueError("snapshot_transport_json_too_deep")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # U+2028/2029 are str line breaks for the consumer's line-oriented YAML reader;
    # the JSON escape is equivalent and untouched by the gh sanitizer.
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    encoded = _GH_BACKSLASH_BEFORE_CTRL_ESCAPE_RE.sub(
        lambda m: "\\u005c" if m.group(1) else m.group(0), encoded
    )
    return encoded


def _json_compact(value: Any) -> str:
    """``_json_compact_measure`` bounded by the consumer parser's per-collection cap."""
    encoded = _json_compact_measure(value)
    if len(encoded) > _PARSER_MAX_JSON_CHARS:
        raise ValueError("snapshot_transport_json_too_large")
    return encoded


def _gh_stable_verbose_text(text: str) -> str:
    """Lossy display form (literal ``\\xNN``) of real control characters."""
    return _GH_UNSAFE_CONTROL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _gh_stable_verbose_value(value: Any) -> Any:
    if isinstance(value, str):
        return _gh_stable_verbose_text(value)
    if isinstance(value, list):
        return [_gh_stable_verbose_value(item) for item in value]
    if isinstance(value, dict):
        converted: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = _gh_stable_verbose_value(key)
            if new_key in converted:
                raise ValueError("snapshot_transport_verbose_key_collision")
            converted[new_key] = _gh_stable_verbose_value(item)
        return converted
    return value


def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint (Issue #1537)
# ---------------------------------------------------------------------------


def _import_allowed_paths_gate_module():
    """Import allowed_paths_review_gate from pr-review-judge scripts dir.

    Single source of truth for Allowed Paths normalization/hash rules
    (AllowedPathsMatcher, AllowedPathsGateEvaluator.compute_allowed_paths_hash)
    so the fingerprint's allowed_paths_normalized_sha256 is computed
    identically by producer (this module) and the reviewer's gate.
    """
    spec = importlib.util.spec_from_file_location(
        "allowed_paths_review_gate_for_ensure_contract_snapshot",
        _ALLOWED_PATHS_REVIEW_GATE_PY,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load allowed_paths_review_gate")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def extract_allowed_paths_from_body(body: str) -> list[str]:
    """Use the contract-review canonical Allowed Paths grammar."""
    spec = importlib.util.spec_from_file_location(
        "baseline_vc_preflight_allowed_paths", _BASELINE_VC_PREFLIGHT_PY
    )
    if spec is None or spec.loader is None:
        raise ValueError("canonical_allowed_paths_extractor_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    paths = module.extract_allowed_paths(body or "")
    return paths if isinstance(paths, list) else []


def _canonicalize_allowed_paths_strict(allowed_paths: list[str]) -> list[str]:
    """Match the review gate's normalization or fail before any POST."""
    gate_mod = _import_allowed_paths_gate_module()
    canonicalized: list[str] = []
    for pattern in allowed_paths:
        normalized = gate_mod.AllowedPathsMatcher.normalize_allowed_pattern(pattern)
        if normalized is None:
            raise ValueError(f"invalid_allowed_path_pattern:{pattern}")
        canonicalized.append(normalized)
    if not canonicalized:
        raise ValueError("allowed_paths_missing_or_empty")
    return sorted(set(canonicalized))


def compute_expected_contract_fingerprint(
    *,
    issue_number: int,
    contract_source_id: str,
    contract_body_sha256: str,
    allowed_paths: list[str],
    base_ref: str,
    base_sha_at_snapshot: str,
) -> dict[str, Any]:
    """
    Compute the 7-item expected_contract_fingerprint dict.

    allowed_paths_normalized_sha256 reuses
    AllowedPathsGateEvaluator.compute_allowed_paths_hash() verbatim (via the
    shared AllowedPathsMatcher normalization) so it is byte-for-byte
    reproducible by the reviewer's independent recomputation (AC4).
    """
    canonicalized = _canonicalize_allowed_paths_strict(allowed_paths)
    normalized_json = json.dumps(
        canonicalized, separators=(",", ":"), ensure_ascii=True
    )
    allowed_paths_hash = hashlib.sha256(normalized_json.encode()).hexdigest()
    return {
        "issue_number": issue_number,
        "contract_source_kind": "issue_comment",
        "contract_source_id": contract_source_id,
        "contract_body_sha256": contract_body_sha256,
        "allowed_paths_normalized_sha256": allowed_paths_hash,
        "base_ref": base_ref,
        "base_sha_at_snapshot": base_sha_at_snapshot,
    }


def capture_base_ref_and_sha(
    repo: str, timeout: int = _DEFAULT_TIMEOUT
) -> tuple[Optional[str], Optional[str]]:
    """
    Capture the repository default branch name and its current tip SHA via
    the GitHub API, for binding a materialized snapshot's fingerprint to a
    concrete (base_ref, base_sha_at_snapshot) pair (AC1).

    Returns (base_ref, base_sha_at_snapshot); either may be None on failure
    -- callers must treat (None, ...) or (..., None) as a capture failure and
    must not materialize a go without both values.
    """
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        return None, None

    query = (
        "query($owner:String!, $name:String!) { "
        "repository(owner:$owner, name:$name) { "
        "defaultBranchRef { name target { ... on Commit { oid } } } "
        "} }"
    )
    try:
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"query={query}",
                "-F", f"owner={owner}",
                "-F", f"name={name}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, Exception):
        return None, None
    if result.returncode != 0:
        return None, None

    try:
        payload = json.loads(result.stdout)
        default_ref = payload["data"]["repository"]["defaultBranchRef"]
        base_ref = default_ref["name"]
        base_sha = default_ref["target"]["oid"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None, None
    if not isinstance(base_ref, str) or not base_ref:
        return None, None
    if not isinstance(base_sha, str) or not base_sha:
        return None, None
    return base_ref, base_sha


def patch_comment(
    issue_number: int,
    repo: str,
    comment_id: int,
    body: str,
    expected_body_text: Optional[str] = None,
    diagnostics_out: Optional[dict[str, Any]] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, Optional[str]]:
    """
    Send one canonical PATCH for an already-posted comment's final body (AC1
    step 2 of the two-phase materialize flow).  A zero PATCH subprocess exit
    proves mutation transport success only: its response representation is not
    application-level authority.  Never blind-retry PATCH.  Immediately use
    the existing authoritative direct-GET full-binding verifier; success
    requires matching comment id, Issue binding, html_url, trusted publisher,
    and the decoded Markdown body's UTF-8 SHA256.  Timeout and transport
    ambiguity use that same GET reconciliation and remain fail-closed unless
    the binding is confirmed.

    #1513 P2 fix_delta: when the caller holds the expected final body text in
    scope, it may pass `expected_body_text` and a `diagnostics_out` dict so
    that a `binding_body_hash_mismatch` on this internal GET reconciliation
    (across all three call sites below) also populates bounded byte-level
    diagnostics (`expected_byte_len` / `actual_byte_len` /
    `first_diff_byte_offset` / `expected_sha` / `actual_sha`), matching the
    same pattern already used by ensure_contract_snapshot's own staging and
    final binding checks. The public return type is unchanged.
    """
    try:
        payload = json.dumps({"body": body}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result = subprocess.run(
            [
                "gh", "api", "--silent", "--method", "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}", "--input", "-",
            ],
            input=payload,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            # A successful subprocess only proves that gh accepted the PATCH
            # response. Its representation is intentionally non-authoritative:
            # invalid JSON and id/body mismatches therefore share this same
            # one-GET reconciliation path with an exact representation. Never
            # retry PATCH; the direct binding verifier is the authority for
            # comment id, issue, html_url, publisher, and decoded body hash.
            ok, err = verify_controlled_publisher_comment_id_binding(
                issue_number,
                repo,
                comment_id,
                expected_body_sha256=sha256_of(body),
                expected_body_text=expected_body_text,
                diagnostics_out=diagnostics_out,
                timeout=timeout,
            )
            return (True, None) if ok else (False, f"patch_get_reconciliation_failed:{err}")

        # The remote may have applied the update despite a transport error.
        ok, _err = verify_controlled_publisher_comment_id_binding(
            issue_number,
            repo,
            comment_id,
            expected_body_sha256=sha256_of(body),
            expected_body_text=expected_body_text,
            diagnostics_out=diagnostics_out,
            timeout=timeout,
        )
        return (True, None) if ok else (False, "patch_transport_unreconciled")
    except subprocess.TimeoutExpired:
        ok, _err = verify_controlled_publisher_comment_id_binding(
            issue_number,
            repo,
            comment_id,
            expected_body_sha256=sha256_of(body),
            expected_body_text=expected_body_text,
            diagnostics_out=diagnostics_out,
            timeout=timeout,
        )
        return (True, None) if ok else (False, "patch_timeout_unreconciled")
    except Exception:
        return False, "patch_error"


_CANONICAL_BODY_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def is_go_fresh(go_result: object, expected_body_sha256: str) -> bool:
    """Return whether a parsed go result is canonical and bound to this body."""
    if not isinstance(go_result, dict):
        return False
    inner = go_result.get("inner")
    if not isinstance(inner, dict):
        return False
    body_sha256 = inner.get("body_sha256")
    return (
        isinstance(body_sha256, str)
        and _CANONICAL_BODY_SHA256_RE.fullmatch(body_sha256) is not None
        and body_sha256 == expected_body_sha256
    )


def has_vc_preflight_classifications(go_result: object) -> bool:
    """Return whether a go snapshot carries baseline VC classifications."""
    if not isinstance(go_result, dict):
        return False
    inner = go_result.get("inner")
    if not isinstance(inner, dict):
        return False
    checks = inner.get("checks")
    if not isinstance(checks, dict):
        return False
    vc_preflight = checks.get("vc_preflight")
    if not isinstance(vc_preflight, dict):
        return False
    classifications = vc_preflight.get("classifications")
    return isinstance(classifications, list)


def is_go_current(go_result: object, expected_body_sha256: str) -> bool:
    """Return whether a go snapshot is fresh and complete for loop consumption."""
    if not is_go_fresh(go_result, expected_body_sha256):
        return False
    if not has_vc_preflight_classifications(go_result):
        return False
    inner = go_result.get("inner") if isinstance(go_result, dict) else None
    checks = inner.get("checks") if isinstance(inner, dict) else None
    product_spec_check = checks.get("product_spec_check") if isinstance(checks, dict) else None
    if not isinstance(product_spec_check, dict):
        return False
    if product_spec_check.get("body_sha256") != expected_body_sha256:
        return False
    return (
        evaluate_product_spec_payload(
            product_spec_check,
            body_sha256=expected_body_sha256,
        ).get("routing_action") == "continue"
    )


def _normalize_classifications_for_transport(
    classifications: list[Any], *, omit_verbose: bool
) -> list[Any]:
    """Return a gh-sanitizer-stable copy: verbose keys lossy/omitted, rest exact."""
    normalized: list[Any] = []
    for item in classifications:
        if not isinstance(item, dict):
            if omit_verbose:
                raise ValueError("vc_preflight_classifications_non_object_cannot_compact")
            _reject_gh_unsafe_control(item)
            normalized.append(item)
            continue
        record: dict[Any, Any] = {}
        for key, value in item.items():
            if key in _CLASSIFICATIONS_TRANSPORT_OMIT_KEYS:
                if not omit_verbose:
                    record[key] = _gh_stable_verbose_value(value)
            else:
                _reject_gh_unsafe_control(key)
                _reject_gh_unsafe_control(value)
                record[key] = value
        normalized.append(record)
    return normalized


def compact_vc_preflight_classifications(
    classifications: list[Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Bound snapshot transport while retaining a strict classifications list.

    The complete producer payload is bound by digest and count.  If it cannot
    fit in the parser's bounded JSON transport, only verbose execution output
    is omitted; classification records and every non-verbose consumer field
    remain unchanged.  A compacted list that still exceeds the hard cap is
    rejected rather than converted to an opaque scalar or truncated further.
    """
    original_json = json.dumps(classifications, ensure_ascii=False, separators=(",", ":"))
    metadata: dict[str, Any] = {
        "schema": _CLASSIFICATIONS_TRANSPORT_SCHEMA,
        "classification_count": len(classifications),
        "original_payload_sha256": sha256_of(original_json),
        "compact": False,
        "truncated": False,
    }
    # The size decision is made on the final transport representation (what the
    # parser's bounded reader measures), after the gh-stable normalization.
    normalized = _normalize_classifications_for_transport(classifications, omit_verbose=False)
    if len(_json_compact_measure(normalized)) <= _MAX_CLASSIFICATIONS_TRANSPORT_CHARS:
        return normalized, metadata

    compacted = _normalize_classifications_for_transport(classifications, omit_verbose=True)
    if len(_json_compact_measure(compacted)) > _MAX_CLASSIFICATIONS_TRANSPORT_CHARS:
        raise ValueError("vc_preflight_classifications_compact_payload_too_large")
    metadata["compact"] = True
    metadata["truncated"] = True
    return compacted, metadata


def is_go_base_binding_current(
    go_result: object,
    live_base_ref: Optional[str],
    live_base_sha: Optional[str],
) -> bool:
    """Return whether a fingerprint binds the go comment to the live base.

    ``is_fingerprint_ready_go`` is the schema and source-identity authority.
    This helper deliberately adds the time-sensitive comparison which that
    parser cannot perform: the fingerprint's captured default branch and tip
    must still equal a fresh GitHub API readback before a go comment is reused.
    """
    if not isinstance(live_base_ref, str) or not live_base_ref:
        return False
    if not isinstance(live_base_sha, str) or not live_base_sha:
        return False
    if not isinstance(go_result, dict):
        return False
    inner = go_result.get("inner")
    if not isinstance(inner, dict):
        return False
    fingerprint = inner.get("expected_contract_fingerprint")
    if not isinstance(fingerprint, dict):
        return False
    return (
        fingerprint.get("base_ref") == live_base_ref
        and fingerprint.get("base_sha_at_snapshot") == live_base_sha
    )


def fetch_issue_snapshot(
    issue_number: int,
    repo: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Fetch body + updatedAt in a single gh call.
    Returns (body_text, updated_at_str, error_code_or_None).

    B2: includes updatedAt for atomicity guard.
    """
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(issue_number),
                "--repo", repo,
                "--json", "body,updatedAt",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, None, "gh_issue_view_error"
        data = json.loads(result.stdout)
        return data.get("body", ""), data.get("updatedAt", ""), None
    except subprocess.TimeoutExpired:
        return None, None, "gh_timeout"
    except json.JSONDecodeError:
        return None, None, "gh_json_error"
    except Exception:
        return None, None, "gh_other_error"


def fetch_issue_body(
    issue_number: int,
    repo: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], Optional[str]]:
    """
    Backward-compatible wrapper: returns (body_text, error_code_or_None).
    Internally calls fetch_issue_snapshot.
    """
    body, _updated_at, err = fetch_issue_snapshot(issue_number, repo, timeout)
    return body, err


def verify_snapshot_authority_postcondition(
    *,
    issue_number: int,
    repo: str,
    expected_body_sha256: str,
    expected_updated_at: str,
    expected_comment_id: int,
    expected_comment_body_sha256: str,
    expected_fingerprint: dict[str, Any],
    timeout: int = _DEFAULT_TIMEOUT,
    observed_updated_at_out: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Re-read every authority input immediately before returning ``status: ok``.

    A prior freshness comparison is only an observation, not an authority
    barrier.  This helper binds the returned snapshot to a second read of the
    issue source, the exact trusted comment, its parsed fingerprint, and a
    single-response default-branch ref/tip pair.  Any drift or incomplete
    readback is deliberately fail-closed.

    The comment authority is decided over the *entire* final-readback comment
    set (Issue #3004): the canonical parser's trusted filtering and
    authoritative-result priority apply, so a later trusted ``blocked`` (or a
    later trusted ``go``) supersedes the expected comment.  Untrusted and
    ordinary comments never reject by themselves.

    ``observed_updated_at_out`` (optional) receives the ``updatedAt`` value this
    call actually read under the key ``"updated_at"``; it is an observation
    only and never alters the comparison.
    """
    body, updated_at, issue_err = fetch_issue_snapshot(issue_number, repo, timeout)
    if issue_err:
        return False, f"authority_issue_readback_failed:{issue_err}"
    if observed_updated_at_out is not None:
        observed_updated_at_out["updated_at"] = updated_at
    if sha256_of(body or "") != expected_body_sha256:
        return False, "authority_issue_body_sha256_mismatch"
    # The timestamp difference is held, not returned: every non-time authority
    # input below is verified first, so a time-only mismatch is the only case
    # that reports ``authority_issue_updated_at_mismatch`` (Issue #3004).
    updated_at_mismatch = updated_at != expected_updated_at

    live_base_ref, live_base_sha = capture_base_ref_and_sha(repo, timeout)
    if not live_base_ref or not live_base_sha:
        return False, "authority_base_capture_failed"
    if (
        expected_fingerprint.get("base_ref") != live_base_ref
        or expected_fingerprint.get("base_sha_at_snapshot") != live_base_sha
    ):
        return False, "authority_base_binding_drift"

    try:
        parser_mod = _import_parser_module()
    except Exception:
        return False, "authority_parser_import_failed"
    comments, comments_err = parser_mod.fetch_issue_comments(issue_number, repo, timeout)
    if comments_err:
        return False, f"authority_comments_readback_failed:{comments_err}"
    comment = next(
        (item for item in comments if item.get("id") == expected_comment_id), None
    )
    if not isinstance(comment, dict):
        return False, "authority_comment_missing"
    comment_body = str(comment.get("body") or "")
    comment_body_sha256 = sha256_of(comment_body)
    if comment_body_sha256 != expected_comment_body_sha256:
        return False, "authority_comment_body_sha256_mismatch"
    bound_ok, binding_err = verify_controlled_publisher_comment_id_binding(
        issue_number,
        repo,
        expected_comment_id,
        expected_body_sha256=expected_comment_body_sha256,
        timeout=timeout,
    )
    if not bound_ok:
        return False, f"authority_comment_binding_failed:{binding_err}"

    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    parsed = parser_mod.parse_contract_review_results(comments, issue_url)
    go_result = next(
        (
            item
            for item in parsed
            if item.get("comment_id") == expected_comment_id
            and item.get("status") == "go"
        ),
        None,
    )
    if not isinstance(go_result, dict):
        return False, "authority_comment_not_current_go"
    # Authoritative-result priority over the whole comment set (same rules as
    # existing_go adoption): the latest *trusted* result decides go/blocked, and
    # the latest trusted fingerprint-ready go must be this exact comment.
    # A trusted go that is not fingerprint-ready is never adoptable, so it must
    # not take over "latest" and hide an earlier trusted blocked.
    adoptable = [
        item
        for item in parsed
        if item.get("status") != "go" or item.get("is_fingerprint_ready") is True
    ]
    latest_trusted = parser_mod.find_latest_result(adoptable, trusted_only=True)
    if isinstance(latest_trusted, dict) and latest_trusted.get("status") == "blocked":
        return False, "authority_latest_trusted_result_blocked"
    latest_trusted_go = parser_mod.find_latest_go(
        parsed, trusted_only=True, fingerprint_ready_only=True
    )
    if (
        isinstance(latest_trusted_go, dict)
        and latest_trusted_go.get("comment_id") != expected_comment_id
    ):
        return False, "authority_superseded_by_later_trusted_go"
    inner = go_result.get("inner")
    if (
        not is_go_current(go_result, expected_body_sha256)
        or not parser_mod.is_fingerprint_ready_go(
            inner, expected_comment_id, issue_number
        )
        or not isinstance(inner, dict)
        or inner.get("expected_contract_fingerprint") != expected_fingerprint
    ):
        return False, "authority_comment_fingerprint_mismatch"
    if not is_go_base_binding_current(go_result, live_base_ref, live_base_sha):
        return False, "authority_comment_base_binding_drift"
    if updated_at_mismatch:
        return False, "authority_issue_updated_at_mismatch"
    return True, None


# Issue #3004: bounded read-only reconciliation limited to the freshly
# materialized go.  The re-verification count is a fixed cap; reaching it is a
# failure, never a success.
_MATERIALIZED_GO_UPDATED_AT_RECONCILE_MAX_ATTEMPTS = 3


def reconcile_materialized_go_updated_at_race(
    *,
    issue_number: int,
    repo: str,
    expected_body_sha256: str,
    observed_updated_at: str,
    expected_comment_id: int,
    expected_comment_body_sha256: str,
    expected_fingerprint: dict[str, Any],
) -> tuple[bool, Optional[str]]:
    """Resolve an ``Issue.updatedAt``-only difference after our own POST/PATCH.

    Called only from the ``materialized_go`` path, after
    ``verify_snapshot_authority_postcondition`` failed with
    ``authority_issue_updated_at_mismatch`` and reported the value it actually
    read (``observed_updated_at``).  Each attempt is a complete, read-only
    re-run of the shared verifier (Issue body SHA, comment id, final comment
    body SHA, fingerprint, base ref/SHA and authoritative-result priority are
    all re-checked) expecting that observed value.  Success therefore requires
    the timestamp to be read identically on two consecutive reads *and* every
    other authority input to hold.

    * Any failure other than the timestamp itself is returned immediately; an
      observed real mismatch is never cancelled by a later match.
    * A timestamp that keeps changing exhausts the fixed cap and fails.
    * Never POSTs/PATCHes and never changes the shared verifier's comparison.
    """
    expected_updated_at = observed_updated_at
    for _ in range(_MATERIALIZED_GO_UPDATED_AT_RECONCILE_MAX_ATTEMPTS):
        observation: dict[str, Any] = {}
        ok, err = verify_snapshot_authority_postcondition(
            issue_number=issue_number,
            repo=repo,
            expected_body_sha256=expected_body_sha256,
            expected_updated_at=expected_updated_at,
            expected_comment_id=expected_comment_id,
            expected_comment_body_sha256=expected_comment_body_sha256,
            expected_fingerprint=expected_fingerprint,
            observed_updated_at_out=observation,
        )
        if ok:
            return True, None
        if err != "authority_issue_updated_at_mismatch":
            return False, err
        next_observed = observation.get("updated_at")
        if not isinstance(next_observed, str) or not next_observed:
            return False, err
        expected_updated_at = next_observed
    return False, "authority_issue_updated_at_mismatch"


def compute_comments_digest(comments: list[dict]) -> str:
    """
    Compute a stable digest of comment IDs and updated_at to detect changes.
    """
    digest_input = json.dumps(
        [{"id": c.get("id"), "updated_at": c.get("updated_at")} for c in comments],
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def comment_body_sha256_for_id(
    comments: list[dict], comment_id: object
) -> Optional[str]:
    """Return the pre-barrier body hash for one exact comment id."""
    for comment in comments:
        if comment.get("id") == comment_id:
            return sha256_of(str(comment.get("body") or ""))
    return None


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


def find_idempotency_marker(
    comments: list[dict],
    issue_number: int,
    body_sha256: str,
) -> Optional[str]:
    """
    Check if the idempotency marker with matching body_sha256 already exists.
    Returns html_url if found, None otherwise.
    """
    marker = _IDEMPOTENCY_MARKER_TEMPLATE.format(
        issue=issue_number,
        body_sha256=body_sha256,
    )
    for comment in comments:
        body = comment.get("body", "") or ""
        if marker in body:
            return comment.get("html_url")
    return None


# ---------------------------------------------------------------------------
# HTTP error classification
# ---------------------------------------------------------------------------


def classify_post_http_error(status_code: int) -> str:
    """
    Classify HTTP error from GitHub comment post API.
    403/429/422 → specific codes, no blind retry.
    """
    if status_code == 403:
        return POST_STATUS_PERMISSION_DENIED
    elif status_code == 429:
        return POST_STATUS_RATE_LIMITED
    elif status_code == 422:
        return POST_STATUS_VALIDATION_FAILED
    else:
        return POST_STATUS_AMBIGUOUS


# ---------------------------------------------------------------------------
# GitHub comment posting via REST API (B5)
# ---------------------------------------------------------------------------


def post_comment(
    issue_number: int,
    repo: str,
    body: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], str, Optional[int]]:
    """
    Post a comment to the issue via GitHub REST API.
    Uses 'gh api --method POST' for precise HTTP status classification (B5).

    Returns (html_url_or_None, post_status_code, http_status_or_None).

    post_status_code: posted | permission_denied | rate_limited |
                      validation_failed_or_spam | ambiguous_no_retry

    Transport (Issue #2627 AC3): mirrors ``patch_comment()`` -- the request
    body is serialized once as canonical JSON UTF-8 bytes and piped to
    ``gh api --method POST ... --input -`` via stdin. No temporary file and
    no field-style file upload flag is used, so there is no implicit OS
    text-mode newline translation between "the bytes this function built"
    and "the bytes gh sent". This function's own successful exit and any
    ``.html_url``
    it decodes from the immediate POST response are a POST-staging
    representation only (Issue #2627 AC2): they prove transport acceptance,
    not persisted-body authority. Callers must not treat this return value's
    body content as authoritative; the sole final persisted-body authority
    remains the decoded Markdown body's UTF-8 SHA256 from a subsequent
    direct-GET readback (verify_controlled_publisher_comment_id_binding /
    verify_snapshot_authority_postcondition), exactly as with patch_comment.
    """
    try:
        payload = json.dumps({"body": body}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result = subprocess.run(
            [
                "gh", "api",
                "--method", "POST",
                f"repos/{repo}/issues/{issue_number}/comments",
                "--input", "-",
                "--jq", ".html_url",
            ],
            input=payload,
            capture_output=True,
            timeout=timeout,
        )
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        if result.returncode == 0:
            url = stdout_text.strip() or None
            return url, POST_STATUS_POSTED, None

        # Extract HTTP status from stderr
        http_status = _extract_http_status(stderr_text)
        if http_status:
            # Handle 404/410 as ambiguous_no_retry
            if http_status in (404, 410):
                return None, POST_STATUS_AMBIGUOUS, http_status
            code = classify_post_http_error(http_status)
            return None, code, http_status

        # Transport error: check idempotency via comment re-fetch
        return None, POST_STATUS_AMBIGUOUS, None

    except subprocess.TimeoutExpired:
        return None, "gh_timeout", None
    except Exception:
        return None, POST_STATUS_AMBIGUOUS, None


def _extract_http_status(stderr: str) -> Optional[int]:
    """Extract HTTP status code from gh CLI stderr."""
    import re
    m = re.search(r"HTTP (\d{3})", stderr or "")
    if m:
        return int(m.group(1))
    # Check for error patterns
    if "403" in stderr:
        return 403
    if "429" in stderr:
        return 429
    if "422" in stderr:
        return 422
    return None


# ---------------------------------------------------------------------------
# Controlled publisher comment ID binding (#1475, AC3)
# ---------------------------------------------------------------------------

_COMMENT_ID_FROM_URL_RE = re.compile(r"#issuecomment-(\d+)$")


def extract_comment_id_from_url(url: Optional[str]) -> Optional[int]:
    """Extract the numeric comment id from a GitHub issue comment html_url."""
    if not url:
        return None
    m = _COMMENT_ID_FROM_URL_RE.search(url)
    if not m:
        return None
    return int(m.group(1))


def _first_diff_byte_offset(expected: bytes, actual: bytes) -> int:
    """
    Return the byte index of the first differing byte between two byte
    strings. If one is a strict prefix of the other (no differing byte
    within the shared overlap), return the length of the shorter string
    (the point of divergence).
    """
    overlap = min(len(expected), len(actual))
    for i in range(overlap):
        if expected[i] != actual[i]:
            return i
    return overlap


def verify_controlled_publisher_comment_id_binding(
    issue_number: int,
    repo: str,
    expected_comment_id: Optional[int],
    expected_body_sha256: Optional[str] = None,
    expected_body_text: Optional[str] = None,
    diagnostics_out: Optional[dict[str, Any]] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, Optional[str]]:
    """
    Confirm that the just-posted comment id is retrievable via the single
    comment GitHub REST endpoint (repos/{repo}/issues/comments/{id}) and that
    it is bound to this issue, this trusted publisher identity, and (when
    provided) the exact comment body that was intended to be published.

    Fail-closed (#1475 fix_delta P1 item 3): a missing/invalid id, fetch
    error, invalid payload, id mismatch, issue mismatch, untrusted publisher
    identity, or body hash mismatch all return (False, <reason_code>).

    #1513 AC1/AC2: when the caller holds the expected body text in scope (not
    only its sha256) and passes it via `expected_body_text`, a
    `binding_body_hash_mismatch` also populates `diagnostics_out` (if
    provided) with bounded UTF-8 byte-level diagnostics:
    `expected_byte_len` / `actual_byte_len` / `first_diff_byte_offset` /
    `expected_sha` / `actual_sha`. When `expected_body_text` is not supplied
    (only `expected_body_sha256` is known to the caller), no diagnostics are
    synthesized -- `diagnostics_out` (if provided) is left untouched so no
    dummy byte-length/offset values are ever fabricated.

    This function only re-validates the state of the comment id at the
    moment it is called (publish-time direct GET). It does NOT by itself
    protect a later `existing_go` snapshot-reuse decision made in a
    different process run days/weeks later -- that decision is instead
    protected by requiring a strict identity-tuple match
    (contract_review_result_parser.is_trusted_snapshot_author) on every
    comment considered as an authoritative candidate, not by re-running this
    binding check at reuse-time. See the ensure_contract_snapshot docstring
    and the PR's Safety Claim Matrix for the explicit scope of this
    guarantee ("authored by an allowlisted GitHub account", not "byte-for-
    byte identical to what this specific process instance posted").
    """
    if (
        expected_comment_id is None
        or isinstance(expected_comment_id, bool)
        or not isinstance(expected_comment_id, int)
        or expected_comment_id <= 0
    ):
        return False, "missing_comment_id"
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/comments/{expected_comment_id}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "binding_readback_timeout"
    except Exception:
        return False, "binding_readback_error"

    if result.returncode != 0:
        return False, "binding_readback_error"

    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return False, "binding_readback_invalid_json"
    if not isinstance(payload, dict):
        return False, "binding_readback_invalid_json"

    if payload.get("id") != expected_comment_id:
        return False, "binding_id_mismatch"

    issue_url = str(payload.get("issue_url") or "")
    expected_suffix = f"/repos/{repo}/issues/{issue_number}"
    if not issue_url.endswith(expected_suffix):
        return False, "binding_issue_mismatch"

    html_url = str(payload.get("html_url") or "")
    if not html_url or extract_comment_id_from_url(html_url) != expected_comment_id:
        return False, "binding_html_url_mismatch"

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    author_login = user.get("login")
    author_id = user.get("id")
    author_type = user.get("type")
    author_association = payload.get("author_association")

    try:
        parser_mod = _import_parser_module()
    except Exception:
        return False, "binding_parser_import_error"

    if not parser_mod.is_trusted_snapshot_author(
        author_login,
        author_association,
        author_id=author_id,
        author_type=author_type,
    ):
        return False, "binding_publisher_untrusted"

    if expected_body_sha256:
        body = str(payload.get("body") or "")
        actual_body_sha256 = sha256_of(body)
        if actual_body_sha256 != expected_body_sha256:
            # #1513 AC1/AC2: only compute byte-level diagnostics when the
            # caller supplied the expected body text itself (not only its
            # sha256). No dummy expected_byte_len/first_diff_byte_offset is
            # ever synthesized from the hash alone.
            if expected_body_text is not None and diagnostics_out is not None:
                expected_bytes = expected_body_text.encode("utf-8")
                actual_bytes = body.encode("utf-8")
                diagnostics_out.update(
                    {
                        "expected_byte_len": len(expected_bytes),
                        "actual_byte_len": len(actual_bytes),
                        "first_diff_byte_offset": _first_diff_byte_offset(
                            expected_bytes, actual_bytes
                        ),
                        "expected_sha": expected_body_sha256,
                        "actual_sha": actual_body_sha256,
                    }
                )
            return False, "binding_body_hash_mismatch"

    return True, None


# ---------------------------------------------------------------------------
# run_contract_review_once wrapper
# ---------------------------------------------------------------------------


def run_contract_review_once(
    issue_number: int,
    repo: str,
    mode: str = "static",
    skip_idempotency_check: bool = True,
    evidence_mode: str = "baseline",
    cwd: Optional[str] = None,
    reviewed_head_sha: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Run run_contract_review_once.py as subprocess.
    Returns (result_dict, error_code_or_None).
    """
    cmd = [
        sys.executable,
        str(_RUN_CONTRACT_REVIEW_ONCE_PY),
        "--issue-number",
        str(issue_number),
        "--repo",
        repo,
        "--mode",
        mode,
    ]
    if skip_idempotency_check:
        cmd.append("--skip-idempotency-check")
    if evidence_mode == "current-head":
        if not cwd or not reviewed_head_sha:
            return None, "current_head_requires_cwd_and_reviewed_head_sha"
        cmd.extend([
            "--evidence-mode", "current-head",
            "--cwd", cwd,
            "--reviewed-head-sha", reviewed_head_sha,
        ])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_VC_TIMEOUT,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return None, "no_stdout_from_run_contract_review_once"
        try:
            return json.loads(stdout), None
        except json.JSONDecodeError as exc:
            # subprocess JSON parse failure → runtime_error
            return None, f"json_parse_error:{exc}"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as exc:
        return None, f"subprocess_error:{exc}"


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def ensure_contract_snapshot(
    issue_number: int,
    repo: str,
    mode: str = "check-only",
    do_post: bool = False,
    artifact_dir: Optional[str] = None,
    evidence_mode: str = "baseline",
    cwd: Optional[str] = None,
    reviewed_head_sha: Optional[str] = None,
) -> dict[str, Any]:
    """
    Main logic for ensure_contract_snapshot.

    Returns CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 dict.

    Schema invariant (B3): status: ok implies contract_snapshot_url is not None.
    dry-run / no-post → status: dry_run_would_post (not ok).
    Schema key: post_status (B4).
    """
    result: dict[str, Any] = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "issue_number": issue_number,
        "repo": repo,
        "mode": mode,
        "status": "runtime_error",
        "source": None,
        "contract_snapshot_url": None,
        "post_status": POST_STATUS_NOT_REQUESTED,  # B4: key is post_status
        "http_status": None,
        "body_sha256_at_check": None,
        "body_sha256_at_post": None,
        "issue_updated_at_at_check": None,   # B2
        "issue_updated_at_at_post": None,    # B2
        "comments_digest_at_check": None,
        "comments_digest_at_post": None,
        "idempotency_marker_found": False,
        "errors": [],
        "contract_review_once_result": None,
        "vc_evidence": {"mode": evidence_mode},
        "current_vc_result": None,
    }

    # Load parser module
    try:
        parser_mod = _import_parser_module()
    except Exception as exc:
        result["errors"].append(f"parser_import_error: {exc}")
        result["status"] = "runtime_error"
        return result

    # Build issue URL for validation
    owner_repo = repo.split("/")
    if len(owner_repo) == 2:
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    else:
        issue_url = None

    existing_go_result: Optional[dict[str, Any]] = None
    existing_go_authority: Optional[tuple[str, str, str]] = None
    existing_go_base_binding_drift = False
    # #1513 AC3: distinct from existing_go_base_binding_drift (a candidate
    # DID pass trusted+fingerprint-ready filtering but its base ref/sha is
    # stale) -- this flags the separate case where a status: go comment
    # exists but is excluded from find_latest_go(trusted_only=True,
    # fingerprint_ready_only=True) candidacy itself (untrusted publisher or
    # not fingerprint-ready).
    go_like_candidate_authority_unmet = False

    # Step 1/2: read a candidate snapshot.  A fresh existing go needs one
    # bounded recheck, otherwise a body edit between the two API calls could
    # make an old comment look current.
    for attempt in range(2):
        body, updated_at, snapshot_err = fetch_issue_snapshot(issue_number, repo)
        if snapshot_err:
            result["errors"].append(f"body_fetch_error: {snapshot_err}")
            result["status"] = "runtime_error"
            return result

        body_sha256 = sha256_of(body or "")
        result["body_sha256_at_check"] = body_sha256
        result["issue_updated_at_at_check"] = updated_at

        comments, comments_err = parser_mod.fetch_issue_comments(issue_number, repo)
        if comments_err:
            result["errors"].append(f"comments_fetch_error: {comments_err}")
            result["status"] = "runtime_error"
            return result
        comments_digest = compute_comments_digest(comments)
        result["comments_digest_at_check"] = comments_digest

        results = parser_mod.parse_contract_review_results(
            comments, expected_issue_url=issue_url
        )
        # #1475 (fix_delta P1 item 1): trust filtering must be applied BEFORE
        # go/blocked precedence is decided, not only when selecting a go
        # candidate. Otherwise an untrusted comment posted after a trusted
        # go can still pre-empt it via the "latest blocked wins" branch below.
        #
        # #3012: a trusted ``go`` that is not fingerprint-ready is never
        # adoptable and must not become "latest" ahead of an earlier trusted
        # blocked (same ``adoptable`` semantics as
        # verify_snapshot_authority_postcondition).
        adoptable = [
            item
            for item in results
            if item.get("status") != "go" or item.get("is_fingerprint_ready") is True
        ]
        latest = parser_mod.find_latest_result(adoptable, trusted_only=True)
        try:
            go_result = parser_mod.find_latest_go(
                results, trusted_only=True, fingerprint_ready_only=True
            )
        except TypeError:
            # A legacy parser/test double cannot prove fingerprint readiness.
            # Do not fall back to its trusted-only result: absence of the
            # predicate is non-authoritative by contract.
            go_result = None

        # #1513 AC3: when no trusted+fingerprint-ready go candidate is found,
        # distinguish "no go-like comment exists at all" (no_existing_go_comment,
        # unchanged) from "a go-like comment exists but is excluded from
        # candidacy by trusted_only/fingerprint_ready_only filtering"
        # (go_like_candidate_authority_unmet, additive-only new
        # classification). This inspects the already-parsed `results` list
        # directly (no additional find_latest_go() call) so it does not
        # disturb existing find_latest_go() call-count/side_effect
        # expectations in callers/tests.
        if go_result is None:
            go_like_candidate_authority_unmet = any(
                isinstance(r, dict) and r.get("status") == "go" for r in results
            )

        # latest (trusted) blocked retains precedence over existing-go adoption.
        if latest and latest["status"] == "blocked":
            result["status"] = "blocked_needs_refinement"
            result["source"] = "latest_blocked"
            result["contract_snapshot_url"] = latest["html_url"]
            return result

        # #1475: parser_mod.find_latest_go(..., trusted_only=True) above already
        # excludes untrusted-author results; is_go_current only re-checks freshness.
        # #1537: an existing go lacking a well-formed source-bound
        # expected_contract_fingerprint must never be reused as current --
        # fall through to (re-)materialization instead.
        fingerprint_ready = bool(
            go_result
            and parser_mod.is_fingerprint_ready_go(
                go_result.get("inner", {}), go_result.get("comment_id"), issue_number
            )
        )
        if not is_go_current(go_result, body_sha256) or not fingerprint_ready:
            break

        # Fingerprint schema validity alone is insufficient: reuse must be
        # bound to the current GitHub default branch and its current tip.
        live_base_ref, live_base_sha = capture_base_ref_and_sha(repo)
        if not live_base_ref or not live_base_sha:
            result["errors"].append(
                "base_ref_or_base_sha_capture_failed: cannot reuse an existing go"
            )
            result["status"] = "runtime_error"
            return result
        if not is_go_base_binding_current(
            go_result, live_base_ref, live_base_sha
        ):
            existing_go_base_binding_drift = True
            break

        body_confirm, updated_confirm, confirm_err = fetch_issue_snapshot(issue_number, repo)
        if confirm_err:
            result["errors"].append(f"body_refetch_error: {confirm_err}")
            result["status"] = "runtime_error"
            return result
        if body_confirm == body and updated_confirm == updated_at:
            comment_body_sha256 = comment_body_sha256_for_id(
                comments, go_result["comment_id"]
            )
            if comment_body_sha256 is None:
                result["status"] = "stale_or_conflicting_snapshot"
                result["errors"].append("existing_go_comment_missing_before_authority_check")
                return result
            authority_ok, authority_err = verify_snapshot_authority_postcondition(
                issue_number=issue_number,
                repo=repo,
                expected_body_sha256=body_sha256,
                expected_updated_at=updated_at,
                expected_comment_id=go_result["comment_id"],
                expected_comment_body_sha256=comment_body_sha256,
                expected_fingerprint=go_result["inner"]["expected_contract_fingerprint"],
            )
            if not authority_ok:
                # A body can undergo ABA (change and then return to the same
                # bytes) while Issue.updatedAt advances.  The existing go is
                # no longer authoritative in that case, but auto/dry-run can
                # still obtain a fresh review/materialization.  Do not reuse
                # the old go, and do not treat other authority failures as
                # recoverable.
                if authority_err == "authority_issue_updated_at_mismatch":
                    break
                result["status"] = "stale_or_conflicting_snapshot"
                result["errors"].append(f"existing_go_authority_postcondition_failed:{authority_err}")
                return result
            if evidence_mode != "current-head":
                result["status"] = "ok"
                result["source"] = "existing_go"
                result["contract_snapshot_url"] = go_result["html_url"]
                return result
            # Snapshot reuse and current-head evidence production are independent:
            # preserve the fresh snapshot, then continue to run the producer.
            result["source"] = "existing_go"
            result["contract_snapshot_url"] = go_result["html_url"]
            existing_go_result = go_result
            existing_go_authority = (body_sha256, updated_at, comment_body_sha256)
            break
        if attempt == 1:
            result["status"] = "stale_or_conflicting_snapshot"
            result["errors"].append("issue_changed_during_existing_go_recheck")
            return result

    # No existing go result.  A current-head caller must still produce fresh
    # evidence when a snapshot was found above, even in check-only mode.
    if mode == "check-only" and result["contract_snapshot_url"] is None:
        result["status"] = "human_judgment"
        result["source"] = "readiness_blocked"
        if existing_go_base_binding_drift:
            result["errors"].append(
                "existing_go_base_binding_drift: run issue-contract-review to generate a fresh snapshot"
            )
        elif go_like_candidate_authority_unmet:
            # #1513 AC3: a go-like comment was found by the unfiltered
            # find_latest_go(), but find_latest_go(trusted_only=True,
            # fingerprint_ready_only=True) excluded it (untrusted publisher
            # or not fingerprint-ready) -- distinct from "no candidate at
            # all" (no_existing_go_comment, AC4).
            result["errors"].append(
                "go_like_candidate_authority_unmet: run issue-contract-review to generate a fresh snapshot"
            )
        else:
            result["errors"].append(
                "no_existing_go_comment: run issue-contract-review to generate contract snapshot"
            )
        return result

    # auto or dry-run mode: run contract review once
    review_result, review_err = run_contract_review_once(
        issue_number=issue_number,
        repo=repo,
        mode="static",
        skip_idempotency_check=True,
        evidence_mode=evidence_mode,
        cwd=cwd,
        reviewed_head_sha=reviewed_head_sha,
    )

    result["contract_review_once_result"] = review_result
    if review_result:
        result["vc_evidence"] = review_result.get("vc_evidence", result["vc_evidence"])
        result["current_vc_result"] = review_result.get(
            "current_vc_result", result["current_vc_result"]
        )

    if review_err:
        result["errors"].append(f"run_contract_review_once_error: {review_err}")
        result["status"] = "runtime_error"
        return result

    if review_result is None:
        result["errors"].append("run_contract_review_once_returned_null")
        result["status"] = "runtime_error"
        return result

    review_status = review_result.get("status", "")

    # human_judgment from run_contract_review_once → human_judgment (NOT blocked)
    if review_status == "human_judgment":
        result["status"] = "human_judgment"
        result["source"] = "human_judgment"
        return result

    # runtime_error propagation
    if review_status == "runtime_error":
        result["status"] = "runtime_error"
        result["errors"].extend(review_result.get("errors", []))
        return result

    # blocked → blocked_needs_refinement
    if review_status == "blocked":
        result["status"] = "blocked_needs_refinement"
        result["source"] = "readiness_blocked"
        return result

    if review_status != "go":
        result["status"] = "runtime_error"
        result["errors"].append(f"unexpected_review_status: {review_status}")
        return result

    if result["contract_snapshot_url"] is not None:
        # current-head evidence production can take long enough for main to
        # advance. Re-read the binding before returning an existing snapshot.
        live_base_ref, live_base_sha = capture_base_ref_and_sha(repo)
        if not live_base_ref or not live_base_sha:
            result["status"] = "runtime_error"
            result["errors"].append(
                "base_ref_or_base_sha_capture_failed: cannot reuse an existing go"
            )
            return result
        if (
            existing_go_result is not None
            and existing_go_authority is not None
            and is_go_base_binding_current(
                existing_go_result, live_base_ref, live_base_sha
            )
        ):
            authority_ok, authority_err = verify_snapshot_authority_postcondition(
                issue_number=issue_number,
                repo=repo,
                expected_body_sha256=existing_go_authority[0],
                expected_updated_at=existing_go_authority[1],
                expected_comment_id=existing_go_result["comment_id"],
                expected_comment_body_sha256=existing_go_authority[2],
                expected_fingerprint=existing_go_result["inner"]["expected_contract_fingerprint"],
            )
            if not authority_ok:
                result["status"] = "stale_or_conflicting_snapshot"
                result["contract_snapshot_url"] = None
                result["errors"].append(
                    f"current_head_existing_go_authority_postcondition_failed:{authority_err}"
                )
                return result
            result["status"] = "ok"
            result["source"] = "existing_go"
            return result
        result["contract_snapshot_url"] = None
        result["source"] = None
        existing_go_base_binding_drift = True

    if mode == "check-only":
        result["status"] = "human_judgment"
        result["source"] = "readiness_blocked"
        result["errors"].append(
            "existing_go_base_binding_drift: run issue-contract-review to generate a fresh snapshot"
        )
        return result

    # review_status == go
    # B3: dry-run / no-post → status: dry_run_would_post (NOT ok)
    # status: ok is reserved for cases where contract_snapshot_url is non-null
    if mode == "dry-run" or not do_post:
        result["status"] = "dry_run_would_post"
        result["source"] = "materialized_go"
        result["post_status"] = POST_STATUS_DRY_RUN
        return result

    # auto + --post: prepare comment body
    # Build idempotency marker
    idempotency_marker = _IDEMPOTENCY_MARKER_TEMPLATE.format(
        issue=issue_number,
        body_sha256=body_sha256,
    )

    # Atomicity check (B2): re-fetch body, updatedAt, and comments before posting
    body_post, updated_at_post, snapshot_post_err = fetch_issue_snapshot(issue_number, repo)
    if snapshot_post_err:
        result["errors"].append(f"body_refetch_error: {snapshot_post_err}")
        result["status"] = "runtime_error"
        return result

    body_sha256_post = sha256_of(body_post or "")
    result["body_sha256_at_post"] = body_sha256_post
    result["issue_updated_at_at_post"] = updated_at_post  # B2

    comments_post, comments_post_err = parser_mod.fetch_issue_comments(issue_number, repo)
    if comments_post_err:
        result["errors"].append(f"comments_refetch_error: {comments_post_err}")
        result["status"] = "runtime_error"
        return result

    comments_digest_post = compute_comments_digest(comments_post)
    result["comments_digest_at_post"] = comments_digest_post

    # B2: Stale check — body_sha256 OR updatedAt changed → exit 50
    if body_sha256_post != body_sha256:
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            f"body_sha256_mismatch: initial={body_sha256} post={body_sha256_post}"
        )
        return result

    # Parse comments before treating an updatedAt change as conflicting: adding a
    # fresh go comment can legitimately advance Issue.updatedAt.
    results_post = parser_mod.parse_contract_review_results(
        comments_post, expected_issue_url=issue_url
    )
    # #1475 (fix_delta P1 item 1): trust filtering before precedence, same as
    # the pre-post check above.
    latest_post = parser_mod.find_latest_result(results_post, trusted_only=True)
    if latest_post and latest_post["status"] == "blocked":
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            "blocked_comment_appeared_during_atomicity_window"
        )
        return result

    # Also check if a go comment appeared in the interim
    try:
        go_post = parser_mod.find_latest_go(
            results_post, trusted_only=True, fingerprint_ready_only=True
        )
    except TypeError:
        # See the initial-read compatibility branch above: a parser without
        # fingerprint-ready support remains fail-closed.
        go_post = None
    go_post_fingerprint_ready = bool(
        go_post
        and parser_mod.is_fingerprint_ready_go(
            go_post.get("inner", {}), go_post.get("comment_id"), issue_number
        )
    )
    # The same live base binding is required when a go appeared during the
    # atomicity window. Do not dedupe to a stale candidate, and do not post a
    # fingerprint if its live base cannot be read back.
    base_ref, base_sha_at_snapshot = capture_base_ref_and_sha(repo)
    if not base_ref or not base_sha_at_snapshot:
        result["status"] = "runtime_error"
        result["errors"].append(
            "base_ref_or_base_sha_capture_failed: cannot materialize or reuse a source-bound fingerprint"
        )
        return result
    if (
        is_go_current(go_post, body_sha256_post)
        and go_post_fingerprint_ready
        and is_go_base_binding_current(
            go_post, base_ref, base_sha_at_snapshot
        )
    ):
        body_dedupe, updated_dedupe, dedupe_err = fetch_issue_snapshot(
            issue_number, repo
        )
        if dedupe_err:
            result["errors"].append(f"body_dedupe_refetch_error: {dedupe_err}")
            result["status"] = "runtime_error"
            return result
        if sha256_of(body_dedupe or "") != body_sha256_post:
            result["status"] = "stale_or_conflicting_snapshot"
            result["errors"].append("body_changed_during_fresh_go_dedupe")
            return result
        dedupe_comment_body_sha256 = comment_body_sha256_for_id(
            comments_post, go_post["comment_id"]
        )
        if dedupe_comment_body_sha256 is None:
            result["status"] = "stale_or_conflicting_snapshot"
            result["errors"].append("fresh_go_dedupe_comment_missing_before_authority_check")
            return result
        authority_ok, authority_err = verify_snapshot_authority_postcondition(
            issue_number=issue_number,
            repo=repo,
            expected_body_sha256=body_sha256_post,
            expected_updated_at=updated_dedupe,
            expected_comment_id=go_post["comment_id"],
            expected_comment_body_sha256=dedupe_comment_body_sha256,
            expected_fingerprint=go_post["inner"]["expected_contract_fingerprint"],
        )
        if not authority_ok:
            result["status"] = "stale_or_conflicting_snapshot"
            result["errors"].append(
                f"fresh_go_dedupe_authority_postcondition_failed:{authority_err}"
            )
            return result
        result["status"] = "ok"
        result["source"] = "existing_go"
        result["contract_snapshot_url"] = go_post["html_url"]
        result["post_status"] = POST_STATUS_DEDUPED
        return result

    if updated_at_post and updated_at and updated_at_post != updated_at:
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            f"updated_at_mismatch: initial={updated_at} post={updated_at_post}"
        )
        return result

    # The immediately preceding capture also supplies the fresh fingerprint.
    # Sharing it with the dedupe gate prevents accepting a drifted go and then
    # posting against a different, unobserved base binding.
    try:
        allowed_paths_at_post = extract_allowed_paths_from_body(body_post or "")
        # Validate before POST; a malformed contract must not create even a
        # provisional remote comment.
        _canonicalize_allowed_paths_strict(allowed_paths_at_post)
    except ValueError as exc:
        result["status"] = "runtime_error"
        result["errors"].append(f"allowed_paths_pre_post_validation_failed:{exc}")
        return result

    # Build comment to post (B6: include checks summary).
    # #1537 AC1 (two-phase materialize, step 1): the provisional body omits
    # expected_contract_fingerprint -- contract_source_id (the real GitHub
    # comment id) cannot be known before POST. A comment lacking the
    # fingerprint is never treated as fingerprint-ready by parsers/gates
    # (fail-closed), so this window is safe even if read concurrently.
    # I-4-2 (#66): the final body embeds review_result values that must survive
    # the `gh api` readback unchanged.  Build a final-shaped body now so that an
    # unrepresentable or oversized value stops before any provisional POST.
    try:
        _build_contract_review_comment(
            issue_number=issue_number,
            repo=repo,
            review_result=review_result,
            idempotency_marker=idempotency_marker,
            body_sha256=body_sha256,
            expected_contract_fingerprint=_worst_case_fingerprint(issue_number, body_sha256),
        )
    except ValueError as exc:
        result["status"] = "runtime_error"
        result["errors"].append(f"snapshot_transport_validation_failed:{exc}")
        return result

    comment_body = _build_contract_review_comment(
        issue_number=issue_number,
        repo=repo,
        review_result=review_result,
        idempotency_marker=idempotency_marker,
        body_sha256=body_sha256,
    )

    # Post comment via REST API — B5: 403/429/422 → no retry
    url, post_code, http_status = post_comment(issue_number, repo, comment_body)
    result["post_status"] = post_code  # B4: key is post_status
    result["http_status"] = http_status

    if post_code == POST_STATUS_POSTED:
        # #1475 fix_delta P1 item 3: verify the controlled publisher's
        # comment id is bound to this issue, this trusted publisher
        # identity, AND the exact comment body just posted, via an
        # independent direct-GET readback -- before treating the freshly
        # materialized snapshot as authoritative. Fail-closed on mismatch.
        expected_comment_id = extract_comment_id_from_url(url)
        # Issue #2627 AC2: this is the POST-staging digest -- it binds only
        # the just-POSTed provisional (pre-fingerprint) comment body to this
        # one immediate direct-GET readback. It intentionally does not share
        # a name with the final persisted-body authority digest below: it
        # never gates the eventual materialized_go decision on its own, and
        # is discarded once this provisional binding check completes.
        staging_post_body_sha256 = sha256_of(comment_body)
        staging_binding_diagnostics: dict[str, Any] = {}
        bound_ok, binding_err = verify_controlled_publisher_comment_id_binding(
            issue_number,
            repo,
            expected_comment_id,
            expected_body_sha256=staging_post_body_sha256,
            expected_body_text=comment_body,
            diagnostics_out=staging_binding_diagnostics,
        )
        if not bound_ok:
            result["status"] = "controlled_publisher_binding_failed"
            result["contract_snapshot_url"] = None
            result["errors"].append(
                f"controlled_publisher_binding_failed: {binding_err}"
            )
            # #1513 AC1: this call site holds the expected staging body text
            # in scope, so a binding_body_hash_mismatch carries bounded
            # byte-level diagnostics alongside the reason code.
            if binding_err == "binding_body_hash_mismatch" and staging_binding_diagnostics:
                result["errors"].append(
                    "binding_body_hash_mismatch_diagnostics: "
                    f"expected_byte_len={staging_binding_diagnostics['expected_byte_len']} "
                    f"actual_byte_len={staging_binding_diagnostics['actual_byte_len']} "
                    "first_diff_byte_offset="
                    f"{staging_binding_diagnostics['first_diff_byte_offset']} "
                    f"expected_sha={staging_binding_diagnostics['expected_sha']} "
                    f"actual_sha={staging_binding_diagnostics['actual_sha']}"
                )
            return result

        # #1537 AC1 (two-phase materialize, step 2): the real comment id is
        # now confirmed via independent read-back. Compute the source-bound
        # fingerprint using that confirmed id as contract_source_id, then
        # PATCH the SAME comment with a final body embedding it, and
        # re-verify via a second independent read-back before treating the
        # snapshot as authoritative.
        expected_contract_fingerprint = compute_expected_contract_fingerprint(
            issue_number=issue_number,
            contract_source_id=str(expected_comment_id),
            contract_body_sha256=body_sha256,
            allowed_paths=allowed_paths_at_post,
            base_ref=base_ref,
            base_sha_at_snapshot=base_sha_at_snapshot,
        )
        final_comment_body = _build_contract_review_comment(
            issue_number=issue_number,
            repo=repo,
            review_result=review_result,
            idempotency_marker=idempotency_marker,
            body_sha256=body_sha256,
            expected_contract_fingerprint=expected_contract_fingerprint,
        )
        # #1513 P2 fix_delta: declared before the patch_comment() call so the
        # same dict is passed through to patch_comment()'s own internal GET
        # reconciliation (which may itself be the first GET to observe a
        # binding_body_hash_mismatch on the final PATCHed body) and reused
        # verbatim by the final_bound_ok verification below.
        final_binding_diagnostics: dict[str, Any] = {}
        patch_ok, patch_err = patch_comment(
            issue_number,
            repo,
            expected_comment_id,
            final_comment_body,
            expected_body_text=final_comment_body,
            diagnostics_out=final_binding_diagnostics,
        )
        if not patch_ok:
            result["status"] = "controlled_publisher_binding_failed"
            result["contract_snapshot_url"] = None
            result["errors"].append(f"fingerprint_patch_failed: {patch_err}")
            # #1513 P2 fix_delta: patch_comment()'s internal GET reconciliation
            # may itself observe a binding_body_hash_mismatch on the final
            # PATCHed body (patch_err takes the form
            # "patch_get_reconciliation_failed:binding_body_hash_mismatch").
            # Surface the same bounded byte-level diagnostics format used by
            # the other two binding checks in this function so this failure
            # mode is not silently dropped.
            if patch_err and patch_err.endswith("binding_body_hash_mismatch") and final_binding_diagnostics:
                result["errors"].append(
                    "binding_body_hash_mismatch_diagnostics: "
                    f"expected_byte_len={final_binding_diagnostics['expected_byte_len']} "
                    f"actual_byte_len={final_binding_diagnostics['actual_byte_len']} "
                    "first_diff_byte_offset="
                    f"{final_binding_diagnostics['first_diff_byte_offset']} "
                    f"expected_sha={final_binding_diagnostics['expected_sha']} "
                    f"actual_sha={final_binding_diagnostics['actual_sha']}"
                )
            return result

        # Issue #2627 AC2: this is the final persisted-body authority digest
        # -- distinct from staging_post_body_sha256 above -- and is the value
        # that ultimately gates whether this run may report status: ok. It is
        # reused verbatim as expected_comment_body_sha256 in the final
        # verify_snapshot_authority_postcondition anchor call below so both
        # authority checks are bound to the exact same digest.
        final_persisted_body_sha256 = sha256_of(final_comment_body)
        final_bound_ok, final_binding_err = verify_controlled_publisher_comment_id_binding(
            issue_number,
            repo,
            expected_comment_id,
            expected_body_sha256=final_persisted_body_sha256,
            expected_body_text=final_comment_body,
            diagnostics_out=final_binding_diagnostics,
        )
        if not final_bound_ok:
            result["status"] = "controlled_publisher_binding_failed"
            result["contract_snapshot_url"] = None
            result["errors"].append(
                f"fingerprint_patch_binding_failed: {final_binding_err}"
            )
            # #1513 AC1: this call site holds the expected final (PATCHed)
            # body text in scope, so a binding_body_hash_mismatch carries
            # bounded byte-level diagnostics alongside the reason code.
            if final_binding_err == "binding_body_hash_mismatch" and final_binding_diagnostics:
                result["errors"].append(
                    "binding_body_hash_mismatch_diagnostics: "
                    f"expected_byte_len={final_binding_diagnostics['expected_byte_len']} "
                    f"actual_byte_len={final_binding_diagnostics['actual_byte_len']} "
                    "first_diff_byte_offset="
                    f"{final_binding_diagnostics['first_diff_byte_offset']} "
                    f"expected_sha={final_binding_diagnostics['expected_sha']} "
                    f"actual_sha={final_binding_diagnostics['actual_sha']}"
                )
            return result

        # The POST/PATCH/read-back sequence is not an atomic transaction with
        # the repository default branch or Issue source.  Anchor a final Issue
        # read and immediately re-check every authority input before exposing
        # this URL as an ``ok`` snapshot.
        authority_body, authority_updated_at, authority_snapshot_err = fetch_issue_snapshot(
            issue_number, repo
        )
        if authority_snapshot_err:
            result["status"] = "runtime_error"
            result["contract_snapshot_url"] = None
            result["errors"].append(
                f"materialized_go_authority_anchor_failed:{authority_snapshot_err}"
            )
            return result
        if sha256_of(authority_body or "") != body_sha256:
            result["status"] = "stale_or_conflicting_snapshot"
            result["contract_snapshot_url"] = None
            result["errors"].append("materialized_go_issue_body_changed_before_authority_check")
            return result
        authority_observation: dict[str, Any] = {}
        authority_ok, authority_err = verify_snapshot_authority_postcondition(
            issue_number=issue_number,
            repo=repo,
            expected_body_sha256=body_sha256,
            expected_updated_at=authority_updated_at,
            expected_comment_id=expected_comment_id,
            expected_comment_body_sha256=final_persisted_body_sha256,
            expected_fingerprint=expected_contract_fingerprint,
            observed_updated_at_out=authority_observation,
        )
        # Issue #3004: only on this freshly materialized path (our own POST and
        # PATCH are already done and independently bound) an Issue.updatedAt-only
        # difference gets a bounded, read-only reconciliation.  check-only and
        # existing_go keep rejecting any updatedAt drift.
        if (
            not authority_ok
            and authority_err == "authority_issue_updated_at_mismatch"
            and isinstance(authority_observation.get("updated_at"), str)
            and authority_observation["updated_at"]
        ):
            authority_ok, authority_err = reconcile_materialized_go_updated_at_race(
                issue_number=issue_number,
                repo=repo,
                expected_body_sha256=body_sha256,
                observed_updated_at=authority_observation["updated_at"],
                expected_comment_id=expected_comment_id,
                expected_comment_body_sha256=final_persisted_body_sha256,
                expected_fingerprint=expected_contract_fingerprint,
            )
        if not authority_ok:
            result["status"] = "stale_or_conflicting_snapshot"
            result["contract_snapshot_url"] = None
            result["errors"].append(
                f"materialized_go_authority_postcondition_failed:{authority_err}"
            )
            return result

        # B3: status: ok only when contract_snapshot_url is non-null
        result["status"] = "ok"
        result["source"] = "materialized_go"
        result["contract_snapshot_url"] = url
    elif post_code in (
        POST_STATUS_PERMISSION_DENIED,
        POST_STATUS_RATE_LIMITED,
        POST_STATUS_VALIDATION_FAILED,
        POST_STATUS_AMBIGUOUS,
    ):
        # 403/429/422: no blind retry — set status to human_judgment
        result["status"] = "human_judgment"
        result["errors"].append(
            f"post_api_error: {post_code} (http_status={http_status}) — no retry"
        )
    else:
        result["status"] = "runtime_error"
        result["errors"].append(f"post_error: {post_code}")

    return result


def _build_contract_review_comment(
    issue_number: int,
    repo: str,
    review_result: dict,
    idempotency_marker: str,
    body_sha256: str,
    expected_contract_fingerprint: Optional[dict[str, Any]] = None,
) -> str:
    """
    Build the GitHub comment body for contract review posting.
    Includes checks summary (B6).

    expected_contract_fingerprint (#1537): when provided, embeds the
    source-bound 7-item fingerprint as a sibling key of CONTRACT_REVIEW_RESULT_V1.
    Left as None for the step-1 provisional POST (the real comment id is not
    known yet).  That POST is deliberately a pending-only schema, never a
    syntactically valid CONTRACT_REVIEW_RESULT_V1.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_repo = repo.split("/")
    issue_url = (
        f"https://github.com/{repo}/issues/{issue_number}"
        if len(owner_repo) == 2
        else ""
    )

    if expected_contract_fingerprint is None:
        return f"""{idempotency_marker}

```yaml
CONTRACT_SNAPSHOT_MATERIALIZATION_PENDING_V1:
  issue_number: {issue_number}
  body_sha256: \"{body_sha256}\"
  phase: awaiting_comment_id_binding
```
"""

    # B6: Build checks summary from review_result
    checks = review_result.get("checks", {}) or {}
    readiness_check = checks.get("readiness", "go") or "go"
    blockers_check = checks.get("blockers", "pass") or "pass"
    product_spec_summary = checks.get("product_spec", "pass") or "pass"
    product_spec_check = checks.get("product_spec_check")
    if not isinstance(product_spec_check, dict):
        product_spec_check = {}
    # Advisory JSON flow values are transported as YAML single-quoted scalars.
    # A direct JSON flow value puts JSON string escapes in YAML's double-quoted
    # string grammar, where an untrusted advisory value such as ``\^[[2K``
    # can make PyYAML reject the whole authoritative comment.  The consumer
    # decodes these designated fields with its existing bounded strict-JSON
    # parser; single quotes keep the two escaping grammars separate.
    def json_yaml_scalar(value: Any) -> str:
        encoded = _json_compact(value)
        return "'" + encoded.replace("'", "''") + "'"

    product_spec_check_json = _json_compact(product_spec_check)
    vc_preflight_check = checks.get("vc_preflight", "pass") or "pass"
    vc_preflight_classifications = review_result.get(
        "vc_preflight_classifications", []
    )
    if not isinstance(vc_preflight_classifications, list):
        vc_preflight_classifications = []
    (
        vc_preflight_classifications,
        classifications_transport,
    ) = compact_vc_preflight_classifications(vc_preflight_classifications)
    classifications_json = json_yaml_scalar(vc_preflight_classifications)
    classifications_transport_json = _json_compact(classifications_transport)

    # P0-2 (#1794 PR review): declared_path_overlap (Issue #1680, advisory
    # only) was being dropped when building the authoritative
    # CONTRACT_REVIEW_RESULT_V1 comment -- run_contract_review_once.py
    # produces it in review_result["checks"]["declared_path_overlap"] but
    # this builder only ever forwarded a fixed set of keys. Persist it as a
    # compact JSON string, matching the product_spec_check embedding style,
    # so the authoritative go snapshot round-trips this volatile,
    # OPEN-PR-live observation instead of silently discarding it.
    declared_path_overlap = checks.get("declared_path_overlap")
    if not isinstance(declared_path_overlap, dict):
        declared_path_overlap = {}
    declared_path_overlap_json = json_yaml_scalar(declared_path_overlap)

    # The source-bound fingerprint is authority-bearing, unlike the advisory
    # checks above.  Its transport is exactly one YAML single-quoted scalar
    # containing bounded strict JSON; the parser validates this raw syntax
    # before accepting the decoded mapping on either parser path.
    fingerprint_yaml = (
        "\n  expected_contract_fingerprint: "
        + json_yaml_scalar(expected_contract_fingerprint)
    )

    body = f"""{idempotency_marker}

## Contract Review Result

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "{now}"
  generated_by: issue-contract-review
  issue_url: {issue_url}
  body_sha256: "{body_sha256}"
  checks:
    readiness: {readiness_check}
    blockers: {blockers_check}
    product_spec: {product_spec_summary}
    product_spec_check: {product_spec_check_json}
    vc_preflight:
      decision: {vc_preflight_check}
      classifications: {classifications_json}
      classifications_transport: {classifications_transport_json}
    declared_path_overlap: {declared_path_overlap_json}
  source: ensure_contract_snapshot_auto{fingerprint_yaml}
```
"""
    if len(body) > _MAX_SNAPSHOT_BODY_CHARS:
        raise ValueError("snapshot_body_too_large")
    return body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ensure_contract_snapshot: check/materialize CONTRACT_REVIEW_RESULT_V1 "
            "for impl-review-loop missing_contract_go handling"
        )
    )
    parser.add_argument(
        "--issue-number",
        "--issue",
        dest="issue_number",
        type=int,
        required=True,
        help="GitHub Issue number",
    )
    parser.add_argument("--evidence-mode", choices=["baseline", "current-head"], default="baseline")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--reviewed-head-sha", default=None)
    parser.add_argument(
        "--repo",
        default=_DEFAULT_REPO,
        help="GitHub repo (owner/name)",
    )
    parser.add_argument(
        "--mode",
        choices=["check-only", "auto", "dry-run"],
        default="check-only",
        help=(
            "check-only: inspect only (default). "
            "auto: run contract review if missing. "
            "dry-run: same logic as auto but no GitHub mutation."
        ),
    )
    parser.add_argument(
        "--post",
        action="store_true",
        default=False,
        help="Enable GitHub mutations (comment posting). Only effective in --mode auto.",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="Directory to save artifact JSON",
    )

    args = parser.parse_args()

    if not args.issue_number:
        print(
            json.dumps(
                {
                    "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
                    "status": "runtime_error",
                    "errors": ["--issue-number is required"],
                }
            )
        )
        return 30  # invalid_input

    result = ensure_contract_snapshot(
        issue_number=args.issue_number,
        repo=args.repo,
        mode=args.mode,
        do_post=args.post,
        artifact_dir=args.artifact_dir,
        evidence_mode=args.evidence_mode,
        cwd=args.cwd,
        reviewed_head_sha=args.reviewed_head_sha,
    )

    # Save artifact if requested
    if args.artifact_dir:
        artifact_path = Path(args.artifact_dir) / f"contract-snapshot-{args.issue_number}.json"
        try:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["artifact_path"] = str(artifact_path)
        except Exception as exc:
            result.setdefault("errors", []).append(f"artifact_write_error: {exc}")

    print(json.dumps(result))

    status = result.get("status", "runtime_error")
    if status == "ok":
        return 0
    elif status == "blocked_needs_refinement":
        return 10
    elif status in ("human_judgment", "dry_run_would_post"):
        return 20
    elif status == "stale_or_conflicting_snapshot":
        return 50
    elif status == "controlled_publisher_binding_failed":
        return 60
    else:  # runtime_error or unknown
        return 40


if __name__ == "__main__":
    sys.exit(main())
