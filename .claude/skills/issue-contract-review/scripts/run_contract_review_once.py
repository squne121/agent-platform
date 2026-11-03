#!/usr/bin/env python3
"""
run_contract_review_once.py

既存 preflight scripts への薄い orchestration wrapper。
issue-contract-review の一連のチェックを 1 回実行し、
CONTRACT_REVIEW_ONCE_RESULT_V1 を stdout に compact JSON で返す。

Wrapper status:
  go             — 全チェック pass / status: go
  blocked        — 1 つ以上の決定論的 block
  human_judgment — 分類不能・native dependency 不可・ambiguous fallback
  runtime_error  — subprocess JSON parse 失敗や環境エラー (human_judgment ではない)

Exit codes:
  0  status: go
  1  status: blocked
  2  status: human_judgment
  3  status: runtime_error
  4  input/argument error

stdout: CONTRACT_REVIEW_ONCE_RESULT_V1 compact JSON のみ
stderr: debug/diagnostic messages のみ（stdout には混入しない）

Check execution order (all modes):
  1. contract_readiness_check.py  — readiness needs_fix → blocked
  2. check_blockers.sh            — exit 1 / fallback ambiguous → blocked/human_judgment
  3. check_product_spec_contract.py — applicable+fail → blocked; applicable+human_judgment → human_judgment
  4. baseline_vc_preflight.py     — blocked → blocked; human_judgment → human_judgment
     (vc_preflight is run in all modes, not only execute)

All four checks pass → status: go with checks summary.

Issue #1914 P0-3 (#1940 adversarial review): the Issue body is fetched
EXACTLY ONCE, at the very start of run_once(), and that single snapshot is
threaded through every subsequent step that reads body content (Step 1
idempotency freshness check, Step 2 readiness check, Step 4 product spec
check, Step 4.5 delivery-rollup applicability check, Step 5 VC preflight)
via --body-file / direct in-process reuse. No step independently re-fetches
the body from GitHub. This removes (not merely detects) the TOCTOU window
where Step 2 and Step 4.5 could previously observe two different Issue
body snapshots if the Issue body changed between two separate network
fetches, and their independently-derived judgments were combined into a
single status: go with no equality check between them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

_CONTRACT_READINESS_CHECK_PY = _SCRIPTS_DIR / "contract_readiness_check.py"
_BASELINE_VC_PREFLIGHT_PY = _SCRIPTS_DIR / "baseline_vc_preflight.py"
_CHECK_BLOCKERS_SH = _SCRIPTS_DIR / "check_blockers.sh"
_CHECK_PRODUCT_SPEC_PY = _SCRIPTS_DIR / "check_product_spec_contract.py"
_EVALUATE_PRODUCT_SPEC_GATE_PY = (
    _SCRIPTS_DIR.parent.parent / "impl-review-loop" / "scripts"
)
if str(_EVALUATE_PRODUCT_SPEC_GATE_PY) not in sys.path:
    sys.path.insert(0, str(_EVALUATE_PRODUCT_SPEC_GATE_PY))

from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Issue #1333 AC2/AC3: baseline_vc_preflight.py の DEFAULT_TIMEOUT_SECONDS を
# import し、per-command timeout の単一の正本として参照する（drift防止）。
from baseline_vc_preflight import DEFAULT_TIMEOUT_SECONDS as _VC_PREFLIGHT_PER_COMMAND_TIMEOUT  # noqa: E402

# Issue #1914: reuse the existing strict resolver (no new YAML parser /
# allowlist) to determine whether the target Issue is a canonical parent
# with parent_mode: delivery-rollup and no `## Verification Commands`
# section, so Step 5 below can skip baseline_vc_preflight.py for that case
# (mirrors #1878's skip inside contract_readiness_check.py's own
# execute-mode call, but must be re-derived here because Step 5 invokes
# baseline_vc_preflight.py directly and independently of that internal skip).
_CREATE_ISSUE_SCRIPTS_DIR = _SCRIPTS_DIR.parent.parent / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))

from baseline_vc_preflight import extract_verification_commands_section  # noqa: E402
# Issue #2233 fix_delta P0-1: single root-owned canonical plan producer,
# imported so Step 5 below can compute a `plan_digest` from the SAME
# pinned `body_snapshot` and pass it through to the
# baseline_vc_preflight.py subprocess for cross-process verification,
# instead of unconditionally forcing an explicit `--timeout-seconds`
# override that bypassed the plan's own (possibly `static_policy`
# sourced) per-command budgets.
from baseline_vc_preflight import compute_canonical_vc_plan  # noqa: E402
# Issue #2254 AC1: this process is a root-owned producer of the immutable
# history_snapshot/v1 for its OWN `body_snapshot`, built ONCE and reused
# for the compute_canonical_vc_plan() call Step 5 makes below, and
# propagated to the baseline_vc_preflight.py subprocess via
# --history-snapshot-file, keeping plan_digest convergent.
from baseline_vc_preflight import produce_immutable_history_snapshot  # noqa: E402
from baseline_vc_preflight import (  # noqa: E402
    resolve_repo_root_for_history as _resolve_repo_root_for_history,
)
import vc_runtime_history as _vc_runtime_history  # noqa: E402
# Issue #2232 Scope Delta (OWNER REQUEST_CHANGES P0-1
# https://github.com/squne121/agent-platform/pull/2255#issuecomment-5340600982):
# `compute_canonical_vc_plan()`'s `is_pure` classification (and therefore
# `plan_digest`) now depends on `cwd` / Allowed Paths. This SAME extraction
# helper -- the one `baseline_vc_preflight.py`'s own `_main_impl()` uses to
# derive `allowed_paths_from_body` before calling `compute_canonical_vc_plan()`
# -- is imported here so this parent process supplies the child subprocess's
# EXACT classification context instead of the function's context-free
# defaults, keeping `plan_digest` convergent across the process boundary.
from baseline_vc_preflight import extract_allowed_paths  # noqa: E402
from contract_readiness_check import (  # noqa: E402
    fetch_body_from_github,
    resolve_existing_issue_validation_profile,
    sha256_of,
)
from mrc_contract_parser import parse_machine_readable_contract  # noqa: E402

_DELIVERY_ROLLUP_PARENT_MODE = "delivery-rollup"
_DELIVERY_ROLLUP_SKIP_REASON_CODE = "delivery_rollup_parent_without_verification_commands"


# ---------------------------------------------------------------------------
# Issue #1914 P1-1 (#1940 review): shared applicability result
# ---------------------------------------------------------------------------


class DeliveryRollupApplicability(NamedTuple):
    """Single shared applicability result, computed once from the same
    primitives (resolve_existing_issue_validation_profile,
    parse_machine_readable_contract, extract_verification_commands_section)
    instead of being independently re-derived at each call site. Both the
    boolean predicate used by Step 4.5 and any future caller within this
    module should consume this single result rather than recombining the
    underlying primitives themselves (#1940 P1-1 review: recombining
    primitives at each caller reintroduces the duplicated-policy-logic
    problem #1878 already solved for canonical parents).

    Note: this dataclass is local to run_contract_review_once.py. The
    equivalent skip-decision inside contract_readiness_check.py's own
    execute-mode call (`skip_preflight = is_canonical_parent and not
    parent_has_vc_section`, #1878/#1867) still independently recomputes the
    same primitives — full cross-file de-duplication would require also
    exposing this dataclass from contract_readiness_check.py, which is
    outside this Issue's Allowed Paths (only run_contract_review_once.py is
    listed; contract_readiness_check.py is not) and is therefore left as a
    follow-up recommendation rather than an in-PR Scope Delta.
    """

    applicable: bool
    issue_kind: Optional[str]
    parent_mode: Optional[str]
    reason_code: Optional[str]
    body_sha256: str


def _resolve_delivery_rollup_applicability(body: str) -> DeliveryRollupApplicability:
    """Compute the delivery-rollup Final-Gate-exemption applicability once."""
    body_sha256 = sha256_of(body)
    resolution = resolve_existing_issue_validation_profile(body)
    if resolution.status != "profile" or resolution.canonical_issue_kind != "parent":
        return DeliveryRollupApplicability(
            applicable=False,
            issue_kind=resolution.canonical_issue_kind,
            parent_mode=None,
            reason_code=None,
            body_sha256=body_sha256,
        )
    parsed = parse_machine_readable_contract(body)
    if not parsed.ok or not isinstance(parsed.data, dict):
        return DeliveryRollupApplicability(
            applicable=False,
            issue_kind=resolution.canonical_issue_kind,
            parent_mode=None,
            reason_code="mrc_parse_failed",
            body_sha256=body_sha256,
        )
    parent_mode = parsed.data.get("parent_mode")
    if parent_mode != _DELIVERY_ROLLUP_PARENT_MODE:
        return DeliveryRollupApplicability(
            applicable=False,
            issue_kind=resolution.canonical_issue_kind,
            parent_mode=parent_mode if isinstance(parent_mode, str) else None,
            reason_code=None,
            body_sha256=body_sha256,
        )
    # #2782 AC5: presence-only判定は truthiness ではなく `is not None` を使う。
    # extract_verification_commands_section() の return contract は None
    # （heading 不在）/ ""（heading 存在するが本文空）/ non-empty str
    # （heading 存在し本文あり）を区別する。truthiness では "" と None が
    # 同じ False になり、delivery-rollup parent が空の VC section を持つ
    # だけで（本来 "vc_section_present" とすべきところ）誤って
    # applicable=True（適用除外）側に倒れてしまう。
    if extract_verification_commands_section(body) is not None:
        return DeliveryRollupApplicability(
            applicable=False,
            issue_kind=resolution.canonical_issue_kind,
            parent_mode=parent_mode,
            reason_code="vc_section_present",
            body_sha256=body_sha256,
        )
    return DeliveryRollupApplicability(
        applicable=True,
        issue_kind=resolution.canonical_issue_kind,
        parent_mode=parent_mode,
        reason_code=_DELIVERY_ROLLUP_SKIP_REASON_CODE,
        body_sha256=body_sha256,
    )


def _is_delivery_rollup_parent_without_vc_section(body: str) -> bool:
    """Backward-compatible bool predicate (existing call sites / tests).

    True iff body is a canonical parent (issue_kind: parent) with
    parent_mode: delivery-rollup and no `## Verification Commands` section.
    Delegates to _resolve_delivery_rollup_applicability() so there is a
    single computation, not a second independent re-derivation."""
    return _resolve_delivery_rollup_applicability(body).applicable

# Issue #1333 AC2: _VC_PREFLIGHT_TIMEOUT は per-command timeout の named
# constant から関係式として導出する（単純な独立リテラル引き上げは禁止）。
# _VC_PREFLIGHT_MAX_COMMAND_BUDGET: Issue #1333 の暫定的な wrapper timeout
# budget（直列実行の想定上限コマンド数）。Issue #1328 のような、同一の重い
# VC コマンドが多数の AC から重複参照される構造への根本対策（dedup/replay・
# bounded parallel execution による総実行時間削減）は Issue #1338 で扱う。
# 本定数は #1328 型のケースを timeout 側だけで完全に吸収することを意図した
# ものではない。
# _VC_PREFLIGHT_OVERHEAD_SECONDS: subprocess 起動・JSON parse 等の固定オーバーヘッド。
_VC_PREFLIGHT_MAX_COMMAND_BUDGET = 6
_VC_PREFLIGHT_OVERHEAD_SECONDS = 60
_VC_PREFLIGHT_TIMEOUT = (
    _VC_PREFLIGHT_PER_COMMAND_TIMEOUT * _VC_PREFLIGHT_MAX_COMMAND_BUDGET
    + _VC_PREFLIGHT_OVERHEAD_SECONDS
)
_DEFAULT_TIMEOUT = 30
# contract_readiness_check.py の execute path は baseline_vc_preflight.py を
# 最大120秒まで待機する。この child budget は本 Issue の Allowed Paths 外で
# hard-code されているため、ここではその canonical execute budget を明示して
# outer wrapper が先に timeout しない関係を維持する。
_READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS = 120
# subprocess 起動、body-file の受け渡し、JSON serialization のための wrapper
# overhead。既存の短時間 subprocess budget を使用して独立した magic number を
# 増やさず、nested execute budget より outer timeout が常に大きくなるようにする。
_READINESS_WRAPPER_OVERHEAD_SECONDS = _DEFAULT_TIMEOUT
_DEFAULT_READINESS_TIMEOUT_SECONDS = (
    _READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS
    + _READINESS_WRAPPER_OVERHEAD_SECONDS
)

# Issue #1338 AC9: named constant for the --max-workers value explicitly
# passed to baseline_vc_preflight.py. Bounded parallel execution there is
# restricted to a dedicated safe read-only predicate (`rg` with a fully
# validated bounded path operand, or exact test -f|-d|-s PATH); pnpm/uv run
# pytest/pytest/gh/git/github_metadata_assert always stay serial regardless
# of this value. grep/egrep/fgrep are intentionally EXCLUDED from the
# parallel-eligible predicate (PR #1508 review P0-2): the prior basename-only
# classification allowed them into the pool with no path/stdin validation.
_VC_PREFLIGHT_MAX_WORKERS = 2

_IDEMPOTENCY_MARKER_PREFIX = "<!-- agent-platform:contract-review-once"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run_script(
    cmd: list[str],
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[dict], int, Optional[str]]:
    """
    Run a script and parse stdout as JSON.
    Returns (parsed_json_or_None, exit_code, error_message_or_None).

    subprocess JSON parse failure → runtime_error (NOT human_judgment).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return None, result.returncode, f"no stdout from {cmd[0]}"
        try:
            parsed = json.loads(stdout)
            return parsed, result.returncode, None
        except json.JSONDecodeError as exc:
            # JSON parse failure → runtime_error (AC design: NOT human_judgment)
            return None, result.returncode, f"json_parse_error: {exc}"
    except subprocess.TimeoutExpired:
        return None, -1, "timeout"
    except FileNotFoundError:
        return None, -1, f"script_not_found: {cmd[0]}"
    except Exception as exc:
        return None, -1, f"subprocess_error: {exc}"


def _run_shell_script(
    cmd: list[str],
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """
    Run a shell script (non-JSON output).
    Returns (exit_code, stdout, stderr).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"script_not_found: {cmd[0]}"
    except Exception as exc:
        return -1, "", f"subprocess_error: {exc}"


# ---------------------------------------------------------------------------
# AC12 (#2040): timeout_phase / execution_time_summary
# ---------------------------------------------------------------------------
#
# When a subprocess step in run_once() times out, callers need to know WHICH
# phase timed out (so they can distinguish "the outer run-once/readiness
# budget was too small" from "the vc-preflight budget was too small" from
# "an individual child command hung") and roughly how long it ran before
# being killed. This is a small, bounded summary -- not a full step-by-step
# execution trace -- and it is populated ONLY when a timeout actually
# occurs. run_once() never retries automatically after a timeout; this
# field exists purely to make the cause legible to a human/loop consumer so
# an "unexplained retry" is not the only recourse.
_TIMEOUT_PHASE_RUN_ONCE_READINESS = "run_once_readiness"
_TIMEOUT_PHASE_VC_PREFLIGHT = "vc_preflight"
_TIMEOUT_PHASE_CHILD_COMMAND = "child_command"


def _timeout_execution_summary(
    phase: str, timeout_seconds: int, elapsed_seconds: Optional[float]
) -> dict[str, Any]:
    """Build the bounded execution_time_summary payload for a timed-out step."""
    return {
        "phase": phase,
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": elapsed_seconds,
    }


def _record_timeout(
    result: dict[str, Any],
    phase: str,
    timeout_seconds: int,
    elapsed_seconds: Optional[float],
) -> None:
    """Record timeout_phase / execution_time_summary on `result` in-place.

    Only invoked on the timeout path; normal (non-timeout) runs never set
    these keys, so the existing CONTRACT_REVIEW_ONCE_RESULT_V1 field set is
    unchanged for the common case (additive-only, backward compatible)."""
    result["timeout_phase"] = phase
    result["execution_time_summary"] = _timeout_execution_summary(
        phase, timeout_seconds, elapsed_seconds
    )


# ---------------------------------------------------------------------------
# Idempotency: check for existing go comment
# ---------------------------------------------------------------------------


def _is_current_go_snapshot(go_result: object, expected_body_sha256: str) -> bool:
    """Use the loop consumer's currentness predicate for producer dedupe."""
    import importlib.util

    ensure_path = (
        _SCRIPTS_DIR.parent.parent / "impl-review-loop" / "scripts"
        / "ensure_contract_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("ensure_contract_snapshot", ensure_path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return bool(module.is_go_current(go_result, expected_body_sha256))


def check_existing_go_comment(
    issue_number: int,
    repo: str,
    current_body_sha256: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Return a current, complete go snapshot or None.

    Dedupe is only safe when the existing comment satisfies the same
    currentness predicate consumed by impl-review-loop.

    Issue #1914 P0-3: ``current_body_sha256`` lets the caller reuse the
    single Issue body snapshot fetched once at the top of run_once(),
    instead of this function performing its own independent
    ``gh issue view`` fetch. When not supplied (e.g. direct/legacy callers),
    this function falls back to fetching it itself.
    """
    try:
        from contract_review_result_parser import (
            fetch_issue_comments,
            find_latest_authoritative_go,
            find_latest_result,
            parse_contract_review_results,
        )
    except ImportError:
        # Try absolute path import
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "contract_review_result_parser",
            _SCRIPTS_DIR / "contract_review_result_parser.py",
        )
        if spec is None or spec.loader is None:
            return None, "import_error"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        fetch_issue_comments = mod.fetch_issue_comments
        find_latest_authoritative_go = getattr(
            mod, "find_latest_authoritative_go",
            lambda results: mod.find_latest_go(results, trusted_only=True, fingerprint_ready_only=True),
        )
        find_latest_result = mod.find_latest_result
        parse_contract_review_results = mod.parse_contract_review_results

    repo_parts = repo.split("/")
    if len(repo_parts) == 2:
        owner, repo_name = repo_parts
        issue_url = f"https://github.com/{owner}/{repo_name}/issues/{issue_number}"
    else:
        issue_url = None

    comments, err = fetch_issue_comments(issue_number, repo)
    if err:
        return None, err

    results = parse_contract_review_results(comments, expected_issue_url=issue_url)
    # #1475 (fix_delta P1 item 1): trust filtering must be applied BEFORE
    # go/blocked precedence is decided. An untrusted comment posted after a
    # trusted go must never pre-empt that go, regardless of its status.
    #
    # #3012: a trusted ``go`` that is not fingerprint-ready is never adoptable
    # and must not become "latest" ahead of an earlier trusted blocked (same
    # ``adoptable`` semantics as verify_snapshot_authority_postcondition).
    adoptable = [
        item
        for item in results
        if item.get("status") != "go" or item.get("is_fingerprint_ready") is True
    ]
    latest = find_latest_result(adoptable, trusted_only=True)

    # If the latest (trusted) result is blocked, do not return an existing go
    if latest and latest["status"] == "blocked":
        return None, None

    # #1475: only a trusted-author go snapshot is authoritative for dedupe.
    go = find_latest_authoritative_go(results)
    if go is None:
        return None, None

    if current_body_sha256 is not None:
        current_body_sha256_value = current_body_sha256
    else:
        try:
            issue = subprocess.run(
                ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
                capture_output=True,
                text=True,
                timeout=_DEFAULT_TIMEOUT,
            )
            if issue.returncode != 0:
                return None, "issue_body_fetch_error"
            current_body = json.loads(issue.stdout).get("body", "")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None, "issue_body_fetch_error"

        current_body_sha256_value = sha256_of(current_body)

    if _is_current_go_snapshot(go, current_body_sha256_value):
        return go, None
    return None, None


# ---------------------------------------------------------------------------
# HTTP error classification (for API post calls — 403/429/422 blind retry forbidden)
# ---------------------------------------------------------------------------

# This module does not post comments itself; ensure_contract_snapshot.py handles posting.
# The classification table is here for consistency and is exported.

HTTP_ERROR_CLASSIFICATIONS: dict[int, str] = {
    403: "permission_denied",
    429: "rate_limited",
    422: "validation_failed_or_spam",
}


_FULL_COMMIT_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")


def validate_current_head_envelope(payload: Any, returncode: int) -> list[str]:
    """Return fail-closed validation errors for a producer current-head envelope."""
    if returncode != 0:
        return [f"producer_nonzero_exit:{returncode}"]
    if not isinstance(payload, dict):
        return ["producer_payload_not_object"]

    errors: list[str] = []
    required_scalars = {
        "schema": "baseline_vc_preflight/v1",
        "evidence_mode": "current-head",
        "status": "pass",
    }
    for key, expected in required_scalars.items():
        if payload.get(key) != expected:
            errors.append(f"invalid_{key}")
    if not isinstance(payload.get("generated_at"), str) or not payload["generated_at"]:
        errors.append("missing_generated_at")
    if payload.get("errors") != []:
        errors.append("errors_not_empty")
    if not isinstance(payload.get("results"), list):
        errors.append("missing_results")
    source = payload.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("body_sha256"), str) or not source["body_sha256"]:
        errors.append("missing_source_body_sha256")
    for key in ("fallback_detected", "human_review_required", "stop_condition_triggered"):
        if payload.get(key) is not False:
            errors.append(f"unsafe_{key}")
    for key in ("clean_before", "clean_after"):
        if payload.get(key) is not True:
            errors.append(f"unclean_{key}")
    head_values = [payload.get(key) for key in ("head_sha", "reviewed_head_sha", "head_after_sha")]
    if (
        not all(isinstance(value, str) and _FULL_COMMIT_OID_RE.fullmatch(value) for value in head_values)
        or len(set(head_values)) != 1
    ):
        errors.append("head_sha_mismatch_or_invalid")
    return errors


def classify_http_error(status_code: int) -> str:
    """
    Classify HTTP error code for contract review API calls.
    403/429/422 → no blind retry (ambiguous_no_retry for unknown codes).
    """
    return HTTP_ERROR_CLASSIFICATIONS.get(status_code, "ambiguous_no_retry")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_once(
    issue_number: int,
    repo: str,
    mode: str = "static",
    skip_idempotency_check: bool = False,
    evidence_mode: str = "baseline",
    cwd: str | None = None,
    reviewed_head_sha: str | None = None,
    readiness_timeout_seconds: int = _DEFAULT_READINESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Run issue-contract-review checks once for the given issue.

    Execution order:
      1. contract_readiness_check.py
      2. check_blockers.sh
      3. check_product_spec_contract.py
      4. baseline_vc_preflight.py

    Returns CONTRACT_REVIEW_ONCE_RESULT_V1 dict.
    """
    result: dict[str, Any] = {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "issue_number": issue_number,
        "repo": repo,
        "mode": mode,
        "status": "runtime_error",
        # Issue #1914 P0-1 (#1940 adversarial review): "applicability"
        # distinguishes "the Final Gate's VC-preflight requirement does not
        # apply to this Issue (delivery-rollup parent exemption)" from
        # "the VC preflight requirement applies and was satisfied". A
        # consumer reading only `status: go` cannot make this distinction;
        # this field exists so any consumer inspecting the FULL result can.
        # Value "applicable" is the safe default (Final Gate presumed to
        # apply) unless a specific check below proves otherwise. This is a
        # field-level-only backward-compatible addition: existing readers
        # that only inspect `status` are UNCHANGED in behavior and are NOT
        # made semantically safe by this field alone — they must be updated
        # to read `applicability` to gain the distinction (see Known Gaps
        # in this PR's description; impl-review-loop / implement-issue
        # consumer code documented to read only `status: go` lives outside
        # this Issue's Allowed Paths and was intentionally NOT modified
        # here).
        "applicability": "applicable",
        "source": None,
        "go_comment_url": None,
        "readiness_status": None,
        "readiness_errors": [],
        "vc_preflight_status": None,
        "vc_preflight_classifications": [],
        "vc_evidence": {"mode": evidence_mode},
        "current_vc_result": None,
        "body_sha256": None,
        "checks": {
            "readiness": None,
            "blockers": None,
            "product_spec": None,
            "product_spec_check": None,
            "vc_preflight": None,
        },
        "idempotency_check": {
            "performed": not skip_idempotency_check,
            "existing_go_url": None,
            "deduped": False,
        },
        "errors": [],
    }

    # `run_once()` is also a programmatic API. Reject invalid values before
    # any GitHub or subprocess work so direct callers cannot accidentally
    # disable the readiness timeout that the CLI already validates.
    if (
        not isinstance(readiness_timeout_seconds, int)
        or isinstance(readiness_timeout_seconds, bool)
        or readiness_timeout_seconds <= 0
    ):
        result["errors"].append(
            "invalid_readiness_timeout_seconds: must be a positive integer"
        )
        return result

    # Issue #1914 P0-3 (#1940 review): fetch the Issue body exactly once, at
    # the very start of run_once(), before any check that reads body
    # content. This single snapshot (and its body_sha256) is threaded
    # through Step 1 (idempotency freshness), Step 2 (readiness check),
    # Step 4 (product spec check), Step 4.5 (delivery-rollup applicability),
    # and Step 5 (VC preflight) via --body-file / direct in-process reuse,
    # so no step can independently observe a different body.
    # Issue #2254 fix_delta P0 blocker 3 (OWNER REQUEST_CHANGES
    # https://github.com/squne121/agent-platform/pull/2382#issuecomment-5458281756):
    # resolve the EFFECTIVE cwd for this whole invocation ONCE, up front --
    # mirrors the child (`baseline_vc_preflight.py` `_main_impl()`) receiving
    # `--cwd cwd` ONLY when `evidence_mode == "current-head"` (see the
    # `vc_command.extend([...])` further down), otherwise falling back to
    # its own `args.cwd or "."` default. Using this SAME value for BOTH the
    # history-snapshot producer below AND the Step 5 `plan_digest`
    # computation keeps `command_group_key` (and therefore `plan_digest`)
    # convergent within this one invocation, instead of the snapshot always
    # being produced at cwd="." while a current-head-mode plan computation
    # used a DIFFERENT explicit `cwd`.
    _effective_cwd_for_digest = cwd if (evidence_mode == "current-head" and cwd) else "."
    _repo_root_for_history = _resolve_repo_root_for_history(_effective_cwd_for_digest)

    body_snapshot, body_snapshot_err = fetch_body_from_github(issue_number, repo)
    if body_snapshot_err:
        result["errors"].append(f"body_snapshot_fetch_error: {body_snapshot_err}")
        result["status"] = "runtime_error"
        return result
    body_snapshot_sha256 = sha256_of(body_snapshot)
    result["body_sha256"] = body_snapshot_sha256

    body_snapshot_fd, body_snapshot_path = tempfile.mkstemp(
        suffix=".md", prefix="contract_review_once_body_"
    )
    # Issue #2254: `_history_snapshot` may be `None` (test-safety guard,
    # or history feature disabled) -- only serialize/propagate a REAL
    # snapshot; `None` must behave identically to every pre-#2254 call
    # site (no --history-snapshot-file argv at all).
    _history_snapshot_path: Optional[str] = None
    try:
        with os.fdopen(body_snapshot_fd, "w", encoding="utf-8") as body_snapshot_file:
            body_snapshot_file.write(body_snapshot)

        # Issue #2254 AC1: ONE root-owned read of the local history store
        # for this whole invocation, reused by Step 2 AND Step 5's
        # plan_digest computation below and serialized to a file for both
        # children. Issue #2254 fix_delta P0 blocker 3: produced at the
        # SAME `_effective_cwd_for_digest` the Step 5 plan computation
        # below uses (not an unconditional cwd="."), keeping
        # `command_group_key` convergent for current-head-mode invocations
        # too.
        _history_snapshot = produce_immutable_history_snapshot(
            body_snapshot, cwd=_effective_cwd_for_digest, repo_root=_repo_root_for_history
        )
        if _history_snapshot is not None:
            _history_snapshot_path = body_snapshot_path + ".history-snapshot.json"
            _vc_runtime_history.write_history_snapshot_file(
                _history_snapshot, Path(_history_snapshot_path)
            )

        # Step 1: idempotency check — if existing go exists, return early
        if not skip_idempotency_check:
            existing_go, id_err = check_existing_go_comment(
                issue_number, repo, current_body_sha256=body_snapshot_sha256
            )
            existing_url = existing_go.get("html_url") if existing_go else None
            result["idempotency_check"]["existing_go_url"] = existing_url
            if id_err:
                result["errors"].append(f"idempotency_check_error: {id_err}")
                # non-fatal: continue
            elif existing_go:
                # Already has a valid go comment — return deduped.
                # P0-2 (#1794 PR review): declared_path_overlap observes the
                # *live* OPEN PR set, which can change at any time independent
                # of the issue body. It must therefore be recomputed fresh on
                # every reuse rather than replayed from the saved comment's
                # checks -- a saved value would go stale immediately. This is
                # advisory-only: recomputing it here never changes result["status"].
                result["status"] = "go"
                result["source"] = "existing_go_comment"
                result["go_comment_url"] = existing_url
                result["idempotency_check"]["deduped"] = True
                checks = existing_go.get("inner", {}).get("checks", {})
                result["checks"]["product_spec_check"] = checks.get("product_spec_check")
                saved_vc_preflight = checks.get("vc_preflight")
                if saved_vc_preflight is not None:
                    result["checks"]["vc_preflight"] = saved_vc_preflight
                # Issue #1914 P0-1: propagate the not_applicable marker from
                # the saved snapshot too, so a deduped delivery-rollup go
                # remains distinguishable from a deduped normal-pass go.
                result["applicability"] = (
                    "not_applicable" if saved_vc_preflight == "not_applicable" else "applicable"
                )
                result["checks"]["declared_path_overlap"] = _run_declared_path_overlap_check(
                    issue_number, repo
                )
                return result

        # Step 2: contract_readiness_check.py (static check)
        readiness_cmd = [
            sys.executable,
            str(_CONTRACT_READINESS_CHECK_PY),
            "--issue",
            str(issue_number),
            "--repo",
            repo,
            "--mode",
            mode if mode in ("static", "preflight-static", "execute") else "static",
            "--body-file",
            body_snapshot_path,
        ]
        # Issue #2254 fix_delta P0 blocker 1 (OWNER REQUEST_CHANGES
        # https://github.com/squne121/agent-platform/pull/2382#issuecomment-5458281756):
        # in `--mode execute`, contract_readiness_check.py ALSO runs
        # baseline_vc_preflight.py internally (via run_baseline_vc_preflight()).
        # Without this, that internal run independently re-read the history
        # store at a DIFFERENT point in time than the snapshot Step 5 below
        # uses for the SAME invocation. Passing the SAME
        # `_history_snapshot_path` here keeps both executions of
        # baseline_vc_preflight.py within this ONE invocation bound to the
        # SAME immutable snapshot (Issue #2254 AC1).
        if _history_snapshot_path is not None:
            readiness_cmd.extend(["--history-snapshot-file", _history_snapshot_path])

        _readiness_started_at = time.monotonic()
        readiness_json, readiness_rc, readiness_err = _run_script(
            readiness_cmd,
            timeout=readiness_timeout_seconds,
        )
        _readiness_elapsed = time.monotonic() - _readiness_started_at

        if readiness_err:
            readiness_error = f"readiness_check_error: {readiness_err}"
            if readiness_err == "timeout":
                readiness_error += (
                    " (readiness_timeout_seconds="
                    f"{readiness_timeout_seconds})"
                )
                _record_timeout(
                    result,
                    _TIMEOUT_PHASE_RUN_ONCE_READINESS,
                    readiness_timeout_seconds,
                    round(_readiness_elapsed, 3),
                )
            result["errors"].append(readiness_error)
            result["status"] = "runtime_error"
            return result

        if readiness_json is None:
            result["errors"].append("readiness_check_no_output")
            result["status"] = "runtime_error"
            return result

        readiness_status = readiness_json.get("status", "")
        result["readiness_status"] = readiness_status
        result["readiness_errors"] = readiness_json.get("errors", [])

        # Map readiness status
        if readiness_status == "human_judgment":
            result["checks"]["readiness"] = "human_judgment"
            result["status"] = "human_judgment"
            result["source"] = "readiness_check"
            return result
        elif readiness_status == "needs_fix":
            result["checks"]["readiness"] = "needs_fix"
            result["status"] = "blocked"
            result["source"] = "readiness_check"
            return result
        elif readiness_status != "go":
            # Unknown status from readiness check
            result["status"] = "runtime_error"
            result["errors"].append(f"unknown_readiness_status: {readiness_status}")
            return result
        else:
            result["checks"]["readiness"] = "go"

        # Step 3: check_blockers.sh
        _blockers_started_at = time.monotonic()
        blockers_rc, blockers_stdout, blockers_stderr = _run_shell_script(
            ["bash", str(_CHECK_BLOCKERS_SH), str(issue_number), repo],
            timeout=_DEFAULT_TIMEOUT,
        )
        _blockers_elapsed = time.monotonic() - _blockers_started_at

        if blockers_rc == -1:
            # Script not found or timeout
            result["errors"].append(f"check_blockers_error: {blockers_stderr}")
            result["status"] = "runtime_error"
            if blockers_stderr == "timeout":
                _record_timeout(
                    result,
                    _TIMEOUT_PHASE_CHILD_COMMAND,
                    _DEFAULT_TIMEOUT,
                    round(_blockers_elapsed, 3),
                )
            return result
        elif blockers_rc == 0:
            result["checks"]["blockers"] = "pass"
        else:
            # exit 1 from check_blockers.sh:
            #   "blocker が open" → deterministic blocked
            #   "native dependency API unavailable" / "不一致" (mismatch) → human_judgment
            stderr_lower = blockers_stderr.lower()
            # Detect truly-ambiguous cases: API unavailable with no fallback, or mismatch
            is_ambiguous = (
                "unavailable" in stderr_lower
                or "mismatch" in stderr_lower
                or "不一致" in blockers_stderr
                or "ambiguous" in stderr_lower
            )
            if is_ambiguous:
                result["checks"]["blockers"] = "human_judgment"
                result["status"] = "human_judgment"
                result["source"] = "check_blockers"
                result["errors"].append(f"check_blockers_human_judgment: {blockers_stderr.strip()}")
                return result
            else:
                # blocker open OR fallback-based determination
                result["checks"]["blockers"] = "blocked"
                result["status"] = "blocked"
                result["source"] = "check_blockers"
                result["errors"].append(f"check_blockers_blocked: {blockers_stderr.strip()}")
                return result

        # Step 4: check_product_spec_contract.py
        _product_spec_started_at = time.monotonic()
        product_spec_json, product_spec_rc, product_spec_err = _run_script(
            [
                sys.executable,
                str(_CHECK_PRODUCT_SPEC_PY),
                "--issue-number",
                str(issue_number),
                "--repo",
                repo,
                "--body-file",
                body_snapshot_path,
            ],
            timeout=_DEFAULT_TIMEOUT,
        )
        _product_spec_elapsed = time.monotonic() - _product_spec_started_at

        if product_spec_err:
            result["errors"].append(f"product_spec_check_error: {product_spec_err}")
            result["status"] = "runtime_error"
            if product_spec_err == "timeout":
                _record_timeout(
                    result,
                    _TIMEOUT_PHASE_CHILD_COMMAND,
                    _DEFAULT_TIMEOUT,
                    round(_product_spec_elapsed, 3),
                )
            return result

        if product_spec_json is None:
            result["errors"].append("product_spec_check_no_output")
            result["status"] = "runtime_error"
            return result

        if product_spec_rc not in (0, 1):
            result["errors"].append(
                f"product_spec_check_nonzero_exit: rc={product_spec_rc}"
            )
            result["status"] = "runtime_error"
            return result

        gate = evaluate_product_spec_payload(
            product_spec_json,
            issue_url=f"https://github.com/{repo}/issues/{issue_number}",
            body_sha256=product_spec_json.get("body_sha256") if isinstance(product_spec_json, dict) else None,
            exit_code=product_spec_rc,
        )
        ps_applicability = gate.get("applicability")
        ps_decision = gate.get("decision")

        if gate.get("routing_action") == "refresh_contract_snapshot":
            result["errors"].append(
                f"product_spec_check_invalid_output: {gate.get('reason', 'unknown')}"
            )
            result["status"] = "runtime_error"
            return result

        # Preserve the validated evaluator payload for consumers that need to
        # distinguish a legacy scalar summary from a schema-valid Product Spec
        # decision bound to this review run.
        result["checks"]["product_spec_check"] = product_spec_json

        if ps_applicability == "applicable":
            if ps_decision == "fail":
                result["checks"]["product_spec"] = "fail"
                result["status"] = "blocked"
                result["source"] = "product_spec_check"
                result["errors"].append(
                    f"product_spec_check_fail: {json.dumps(product_spec_json.get('blocked_reasons', []))}"
                )
                return result
            elif ps_decision == "human_judgment":
                result["checks"]["product_spec"] = "human_judgment"
                result["status"] = "human_judgment"
                result["source"] = "product_spec_check"
                return result
            else:
                result["checks"]["product_spec"] = "pass"
        else:
            # not_applicable → treat as pass
            result["checks"]["product_spec"] = "pass"

        # Step 4.5 (Issue #1914): delivery-rollup parent applicability check.
        # A canonical parent Issue (issue_kind: parent) with
        # parent_mode: delivery-rollup and no `## Verification Commands`
        # section is exempt from the Final Gate's baseline_vc_preflight
        # requirement (OWNER decision, Issue #1890 / #1914). baseline_vc_preflight.py
        # itself is not modified; Step 5 below is simply not invoked for this case.
        #
        # P0-3 fix: this reuses body_snapshot (the SAME single fetch used by
        # Step 2 above) directly in-process. There is no second
        # fetch_body_from_github() call here anymore, so Step 2 and Step 4.5
        # are now structurally guaranteed to observe identical body bytes.
        delivery_rollup_applicability = _resolve_delivery_rollup_applicability(body_snapshot)
        if delivery_rollup_applicability.applicable:
            result["checks"]["vc_preflight"] = "not_applicable"
            result["vc_preflight_status"] = "not_applicable"
            result["vc_preflight_classifications"] = []
            result["applicability"] = "not_applicable"
            result["checks"]["declared_path_overlap"] = _run_declared_path_overlap_check(
                issue_number, repo
            )
            result["status"] = "go"
            result["source"] = _DELIVERY_ROLLUP_SKIP_REASON_CODE
            return result

        # Step 5: baseline_vc_preflight.py (run in all modes)
        # Issue #2233 fix_delta P0-1 (OWNER merge_blocker 1): this call site
        # MUST NOT unconditionally pass `--timeout-seconds` -- doing so
        # forced `source: explicit_override` on every command, silently
        # discarding the canonical plan's own per-command budgets (in
        # particular any `static_policy`-sourced budget above
        # DEFAULT_PER_COMMAND_TIMEOUT_SECONDS). No operator-facing override
        # flag exists on THIS script's own CLI (see argparse below), so
        # `--timeout-seconds` is never passed here; the child resolves its
        # own plan-derived budget per command.
        # Issue #2232 Scope Delta P0-1: the child (`baseline_vc_preflight.py`
        # `_main_impl()`) only ever receives `--cwd cwd` when
        # `evidence_mode == "current-head"` (see the `vc_command.extend([...])`
        # below); otherwise its own `args.cwd = args.cwd or "."` default
        # applies. Mirror that EXACT resolution here so this parent-side
        # digest is computed with the SAME `cwd` / Allowed Paths context the
        # child will actually use, instead of the function's context-free
        # defaults that previously caused `vc_plan_digest_mismatch` for
        # Allowed-Paths-sensitive directory `rg` commands.
        _vc_plan_for_digest = compute_canonical_vc_plan(
            body_snapshot,
            cwd=_effective_cwd_for_digest,
            allowed_paths=extract_allowed_paths(body_snapshot),
            history_snapshot=_history_snapshot,
            repo_root=_repo_root_for_history,
        )
        vc_command = [
                sys.executable,
                str(_BASELINE_VC_PREFLIGHT_PY),
                "--issue",
                str(issue_number),
                "--repo",
                repo,
                "--max-workers",
                str(_VC_PREFLIGHT_MAX_WORKERS),
                "--body-file",
                body_snapshot_path,
                "--expected-plan-digest",
                _vc_plan_for_digest["plan_digest"],
        ]
        if _history_snapshot_path is not None:
            vc_command.extend(["--history-snapshot-file", _history_snapshot_path])
        # Issue #2233 fix_delta P0-2: the outer subprocess.run() timeout for
        # THIS invocation must never be smaller than the plan's own
        # aggregate_timeout_seconds (+ margin) -- otherwise a
        # `static_policy`-elevated per-command budget inside the child could
        # legitimately still be running when this wrapper's own timeout
        # fires. `_VC_PREFLIGHT_TIMEOUT` (the #1333/#1338-owned constant
        # below) remains the floor for the common case.
        _vc_preflight_timeout = max(
            _VC_PREFLIGHT_TIMEOUT,
            _vc_plan_for_digest["aggregate_timeout_seconds"] + _VC_PREFLIGHT_OVERHEAD_SECONDS,
        )
        if evidence_mode == "current-head":
            if not cwd or not reviewed_head_sha:
                result["status"] = "blocked"
                result["source"] = "vc_preflight"
                result["checks"]["vc_preflight"] = "blocked"
                result["errors"].append("current_head_requires_cwd_and_reviewed_head_sha")
                return result
            vc_command.extend([
                "--cwd", cwd,
                "--evidence-mode", "current-head",
                "--reviewed-head-sha", reviewed_head_sha,
                "--format", "json",
            ])
        _vc_started_at = time.monotonic()
        vc_result_json, vc_rc, vc_err = _run_script(
            vc_command,
            timeout=_vc_preflight_timeout,
        )
        _vc_elapsed = time.monotonic() - _vc_started_at

        if vc_err:
            result["errors"].append(f"vc_preflight_error: {vc_err}")
            result["status"] = "runtime_error"
            if vc_err == "timeout":
                # Issue #2233 fix_delta: report the ACTUAL timeout used for
                # this invocation (`_vc_preflight_timeout`, which may exceed
                # `_VC_PREFLIGHT_TIMEOUT` when the plan's own
                # aggregate_timeout_seconds is larger), not the stale module
                # constant.
                _record_timeout(
                    result,
                    _TIMEOUT_PHASE_VC_PREFLIGHT,
                    _vc_preflight_timeout,
                    round(_vc_elapsed, 3),
                )
            return result

        if vc_result_json is None:
            result["errors"].append("vc_preflight_no_output")
            result["status"] = "runtime_error"
            return result

        vc_status = vc_result_json.get("status", "")
        result["vc_preflight_status"] = vc_status
        result["vc_preflight_classifications"] = vc_result_json.get("results", [])
        result["current_vc_result"] = vc_result_json
        result["vc_evidence"] = vc_result_json

        if evidence_mode == "current-head":
            envelope_errors = validate_current_head_envelope(vc_result_json, vc_rc)
            if envelope_errors:
                result["checks"]["vc_preflight"] = "blocked"
                result["status"] = "blocked"
                result["source"] = "vc_preflight"
                result["errors"].extend(
                    f"uncertified_current_head_vc_evidence:{error}"
                    for error in envelope_errors
                )
                return result

        if vc_status == "human_judgment":
            result["checks"]["vc_preflight"] = "human_judgment"
            result["status"] = "human_judgment"
            result["source"] = "vc_preflight"
            return result
        elif vc_status == "blocked":
            result["checks"]["vc_preflight"] = "blocked"
            result["status"] = "blocked"
            result["source"] = "vc_preflight"
            return result
        elif vc_status == "pass":
            result["checks"]["vc_preflight"] = "pass"
            # Step 6: declared_path_overlap (Issue #1680, advisory only).
            result["checks"]["declared_path_overlap"] = _run_declared_path_overlap_check(
                issue_number, repo
            )
            result["status"] = "go"
            result["source"] = "all_checks_pass"
            return result
        else:
            # Unknown vc status
            result["status"] = "runtime_error"
            result["errors"].append(f"unknown_vc_preflight_status: {vc_status}")
            return result
    finally:
        try:
            os.unlink(body_snapshot_path)
        except OSError:
            pass
        if _history_snapshot_path is not None:
            try:
                os.unlink(_history_snapshot_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# declared_path_overlap (Issue #1680, advisory only)
# ---------------------------------------------------------------------------


# P0-3 (#1794 PR review): base branch this repo's contract review targets.
# Passed explicitly to compute_declared_path_overlap_for_issue() so the
# OPEN PR inventory is scoped to the same base branch as the Allowed Paths
# review, instead of leaving base_ref unset (P1-3).
_DECLARED_PATH_OVERLAP_BASE_REF = "main"


def _unavailable_declared_path_overlap_result(reason: str) -> dict[str, Any]:
    return {
        "schema": "declared_path_overlap/v1",
        "advisory": True,
        "blocking": False,
        "decision": "unavailable",
        "disjoint": None,
        "overlapping_prs": [],
        "inventory": None,
        "errors": [reason],
    }


def _run_declared_path_overlap_check(issue_number: int, repo: str) -> dict[str, Any]:
    """
    declared_path_overlap (Issue #1680): OPEN PR の changed-file 名と対象
    Issue の Allowed Paths の単純な重なりを advisory のみで記録する。

    これは実 Git merge 競合の証明ではない。3-way merge・hunk 競合・
    rename/delete 競合は評価しない（実 Git merge 競合の判定は Issue #1792 の
    PAIRWISE_MERGE_OBSERVATION_V1 producer と、その呼び出し元配線
    Issue #1793 に分離済み）。単独では blocking にしない — 呼び出し側
    (run_once) はこの check の結果によって status を変えてはならない。

    P0-3 (#1794 PR review): the producer call (dynamic import +
    compute_declared_path_overlap_for_issue()) runs BEFORE result["status"]
    is set to "go" in run_once(). An uncaught exception here previously
    propagated out of run_once() entirely, losing the CLI's JSON stdout
    contract. The whole producer call is now isolated in a try/except so
    any internal failure degrades to an advisory "unavailable" result
    instead of crashing the caller.
    """
    try:
        try:
            from declared_path_overlap import compute_declared_path_overlap_for_issue
        except ImportError:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "declared_path_overlap", _SCRIPTS_DIR / "declared_path_overlap.py"
            )
            if spec is None or spec.loader is None:
                return _unavailable_declared_path_overlap_result("module_load_error")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            compute_declared_path_overlap_for_issue = module.compute_declared_path_overlap_for_issue

        overlap_result = compute_declared_path_overlap_for_issue(
            issue_number, repo, base_ref=_DECLARED_PATH_OVERLAP_BASE_REF
        )
        if not isinstance(overlap_result, dict):
            overlap_result = _unavailable_declared_path_overlap_result(
                "declared_path_overlap_non_dict_result"
            )
    except Exception as exc:
        overlap_result = {
            "schema": "declared_path_overlap/v1",
            "advisory": True,
            "blocking": False,
            "decision": "unavailable",
            "disjoint": None,
            "overlapping_prs": [],
            "inventory": None,
            "errors": [
                f"declared_path_overlap_internal_exception: {type(exc).__name__}: {exc}"
            ],
        }

    # advisory-only の契約を呼び出し側で防御的に強制する: この check が
    # どのような結果を返しても blocking にしてはならない。将来の実装ミスで
    # advisory/blocking フラグが崩れていた場合は、安全側 (advisory=True /
    # blocking=False) に強制上書きし、違反を errors に記録する。
    if overlap_result.get("advisory") is not True or overlap_result.get("blocking") is not False:
        overlap_result["advisory"] = True
        overlap_result["blocking"] = False
        overlap_result.setdefault("errors", []).append(
            "declared_path_overlap_contract_violation_forced_advisory"
        )

    inventory = overlap_result.get("inventory") or {}
    total_count = inventory.get("totalCount")
    if total_count is not None and not inventory.get("complete", False):
        overlap_result.setdefault("errors", []).append(
            f"declared_path_overlap_inventory_incomplete: "
            f"fetched={inventory.get('fetched_count')} totalCount={total_count}"
        )

    return overlap_result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    def positive_int(value: str) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError("must be a positive integer")
        return parsed

    parser = argparse.ArgumentParser(
        description=(
            "run_contract_review_once: run issue-contract-review checks once, "
            "return CONTRACT_REVIEW_ONCE_RESULT_V1 JSON"
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
        default="squne121/agent-platform",
        help="GitHub repo (owner/name)",
    )
    parser.add_argument(
        "--mode",
        choices=["static", "preflight-static", "execute"],
        default="static",
        help="Check mode (default: static)",
    )
    parser.add_argument(
        "--skip-idempotency-check",
        action="store_true",
        default=False,
        help="Skip existing go comment check",
    )
    parser.add_argument(
        "--readiness-timeout-seconds",
        type=positive_int,
        default=_DEFAULT_READINESS_TIMEOUT_SECONDS,
        help=(
            "Timeout in seconds for contract_readiness_check.py only "
            f"(default: {_DEFAULT_READINESS_TIMEOUT_SECONDS})"
        ),
    )

    args = parser.parse_args()

    result = run_once(
        issue_number=args.issue_number,
        repo=args.repo,
        mode=args.mode,
        skip_idempotency_check=args.skip_idempotency_check,
        evidence_mode=args.evidence_mode,
        cwd=args.cwd,
        reviewed_head_sha=args.reviewed_head_sha,
        readiness_timeout_seconds=args.readiness_timeout_seconds,
    )

    print(json.dumps(result))

    status = result.get("status", "runtime_error")
    if status == "go":
        return 0
    elif status == "blocked":
        return 1
    elif status == "human_judgment":
        return 2
    else:  # runtime_error
        return 3


if __name__ == "__main__":
    sys.exit(main())
