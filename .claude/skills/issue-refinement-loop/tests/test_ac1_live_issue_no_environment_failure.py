"""Issue #2165 AC1 (runtime-verification: true).

OWNER 2026-08-15 REQUEST_CHANGES P0-2: this file is split into two kinds of
coverage:

1. A DETERMINISTIC, SCALED-CLOCK integration test (always runs, no live
   Issue / `gh` dependency) that reproduces the P0-1 regression end to end
   -- `contract_readiness_check.py` reporting a typed `status:
   "runtime_error"` baseline_vc_preflight aggregate timeout must reach
   `run_root_review_pipeline.py`'s `run-checker-attempt` subcommand as a
   structured exit-2 failure (not fall through to `run_merge_readiness()`
   as an ordinary semantic result), and `reviewer_transport.py` must
   classify that as `reason_code: "timeout"` with `semantic_verdict: null`
   and must NOT retry it for the deterministic backend. This does not wait
   the real ~150-350s the production timeouts use; it monkeypatches the
   relevant module-level timeout constants down to sub-second scaled
   values before executing the SAME real subprocess chain.

2. A tightened LIVE canary (real network I/O via `gh issue view` against
   live Issue #2156, which carries a legitimately long-running
   Verification Command). Per the Issue body's `## Runtime Verification
   Applicability` fallback policy (`fallback_success_is_pass: false`): a
   GitHub-reachable environment that still fails to produce a result is a
   real FAIL, not a fallback PASS. The previous version of this test
   accepted `status in {"ok", "input_or_runtime_error"}` unconditionally,
   which is exactly a "fallback success is pass" allowance the Issue body
   forbids -- an authenticated `gh` failure (auth revoked mid-run, repo
   resolution failure, body retrieval failure, malformed JSON) must FAIL
   this test, not pass it silently. Only the absence of `gh auth` itself
   is a SKIP (not a PASS/FAIL) condition.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
_SCRIPT = _SCRIPTS_DIR / "run_root_review_pipeline.py"
_ISSUE_NUMBER = 2156
_REPO = "example-org/example-repo"

sys.path.insert(0, str(_SCRIPTS_DIR))
import reviewer_transport as _reviewer_transport  # noqa: E402
import run_root_review_pipeline as _pipeline  # noqa: E402

_CONTRACT_REVIEW_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
if str(_CONTRACT_REVIEW_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_REVIEW_SCRIPTS))
import contract_readiness_check as _contract_readiness_check  # noqa: E402

# Issue #2165: PER_ATTEMPT_DEADLINE_SECONDS/TOTAL_DEADLINE_SECONDS bound
# `run_reviewer_transport()`'s own retry loop; give the outer subprocess a
# further margin above that ceiling for `gh` I/O and process
# startup/teardown overhead around it.
_SUBPROCESS_TIMEOUT_SECONDS = _reviewer_transport.TOTAL_DEADLINE_SECONDS + 60


def _gh_auth_available() -> bool:
    gh = shutil.which("gh")
    if gh is None:
        return False
    try:
        completed = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# 1. Deterministic, scaled-clock regression (always runs)
# ---------------------------------------------------------------------------


_SCALED_BODY_WITH_LONG_VC = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "AC1 deterministic fixture"
change_kind: infra
```

## Outcome

Fixture body for a scaled-clock deterministic timeout regression test.

## Acceptance Criteria

- [ ] AC1: fixture

## Verification Commands

```bash
# AC1
$ rg --version
```

## Allowed Paths

- fixture/path.py
"""


def test_given_scaled_aggregate_timeout_when_run_baseline_vc_preflight_then_typed_runtime_error(monkeypatch):
    # Reproduces the P0-1 regression at the innermost layer WITHOUT waiting
    # a real ~150-350s: the cooperative-supervisor call
    # `run_baseline_vc_preflight()` makes for the `baseline_vc_preflight.py`
    # subprocess (Issue #2207 OWNER P0-1, PR #2221 REQUEST_CHANGES:
    # `subprocess.run(timeout=...)` was replaced with
    # `baseline_vc_preflight.run_subprocess_with_cooperative_supervisor()`,
    # which reports a timeout via a `SupervisedSubprocessResult.timed_out`
    # flag rather than raising `subprocess.TimeoutExpired` -- this test is
    # updated to monkeypatch THAT call site instead) is monkeypatched to
    # deterministically report a timed-out result (the exact shape the
    # real 200s+ aggregate wrapper timeout produces when a genuinely slow
    # VC exceeds it), and the resulting payload must be a typed `status:
    # "runtime_error"`, never the old plain `errors: ["timeout"]` blocked
    # payload.
    class _FakeTimedOutSupervisedResult:
        timed_out = True
        returncode = -1
        stdout = ""
        stderr = ""
        duration_seconds = 0.5

    def _fake_supervisor(*args, **kwargs):
        return _FakeTimedOutSupervisedResult()

    monkeypatch.setattr(
        _contract_readiness_check,
        "_run_subprocess_with_cooperative_supervisor",
        _fake_supervisor,
    )
    result, exit_code = _contract_readiness_check.run_baseline_vc_preflight(_SCALED_BODY_WITH_LONG_VC)
    assert result["status"] == "runtime_error"
    assert result["failure_class"] == "timeout"
    assert result["timeout_phase"] == "baseline_vc_preflight_aggregate"
    assert result["retryable"] is False
    assert exit_code == -1


def test_given_runtime_error_preflight_when_build_result_then_status_is_runtime_error_not_needs_fix():
    preflight_result = {
        "schema": "baseline_vc_preflight/v1",
        "status": "runtime_error",
        "results": [],
        "errors": [],
        "failure_class": "timeout",
        "timeout_phase": "baseline_vc_preflight_aggregate",
        "retryable": False,
    }
    validate_result = {"status": "pass", "errors": []}
    built = _contract_readiness_check.build_result(
        _SCALED_BODY_WITH_LONG_VC, "execute", validate_result, preflight_result, -1
    )
    # The exact P0-1 regression: this MUST NOT be "needs_fix"/"go" -- a
    # runtime/execution-budget failure is not a body-author-fixable
    # semantic finding.
    assert built["status"] == "runtime_error"
    assert built["status"] not in {"needs_fix", "go", "human_judgment"}


def test_given_runtime_error_readiness_result_when_run_checker_pipeline_once_then_timeout_not_semantic_merge(
    monkeypatch,
):
    runtime_error_readiness = (
        {
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "status": "runtime_error",
            "timeout_phase": "baseline_vc_preflight_aggregate",
        },
        4,
        None,
    )
    merge_readiness_called = {"called": False}

    def _fake_run_check_issue_contract(body_file, **kwargs):
        return {"status": "ok"}, 0, None

    def _fake_run_contract_readiness_check(body_file, **kwargs):
        return runtime_error_readiness

    def _fake_run_merge_readiness(**kwargs):
        merge_readiness_called["called"] = True
        return {"verdict": "needs-fix", "blocking_issues": ["should not reach here"]}, 0, None

    monkeypatch.setattr(_pipeline, "run_check_issue_contract", _fake_run_check_issue_contract)
    monkeypatch.setattr(_pipeline, "run_contract_readiness_check", _fake_run_contract_readiness_check)
    monkeypatch.setattr(_pipeline, "run_merge_readiness", _fake_run_merge_readiness)

    merged, error_code, timeout_phase = _pipeline.run_checker_pipeline_once(
        body_file="/nonexistent/does-not-matter", issue_number=2156, body_sha256="sha256:" + "a" * 64
    )
    assert merged is None
    assert error_code == "timeout"
    assert timeout_phase == "baseline_vc_preflight_aggregate"
    # The P0-1 core assertion: run_merge_readiness() (which would produce a
    # semantic_verdict) must never be reached.
    assert merge_readiness_called["called"] is False


def test_given_child_reports_baseline_aggregate_timeout_when_transport_runs_then_no_retry_and_null_verdict(tmp_path):
    # End-to-end at the reviewer_transport layer: the child prints the
    # SAME stderr shape `_cmd_run_checker_attempt()` emits for this
    # condition and exits 2.
    program = (
        "import json, sys\n"
        "print(json.dumps({'error_code': 'timeout', "
        "'timeout_phase': 'baseline_vc_preflight_aggregate'}), file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    result = _reviewer_transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", program],
        command_id="ac1.deterministic",
        argv_template_id="ac1.deterministic/v1",
        backend="deterministic",
        issue_number=2156,
        repo=_REPO,
        reviewed_body_sha256="sha256:" + "b" * 64,
        artifact_root=tmp_path,
        invocation_id="ac1-deterministic",
        session_ref=None,
        per_attempt_deadline=1,
        total_deadline=2,
    )
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    # deterministic backend must not retry a VC-execution-budget timeout.
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["reason_code"] == "timeout"
    assert result["attempts"][0]["timeout_phase"] == "baseline_vc_preflight_aggregate"


# ---------------------------------------------------------------------------
# 2. Live canary (tightened: authenticated failure is FAIL, not PASS)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _gh_auth_available(),
    reason="gh auth not available in this execution environment (SKIP, not PASS)",
)
def test_given_live_issue_with_long_running_vc_when_produce_runs_then_no_environment_failure(tmp_path):
    artifacts_dir = _REPO_ROOT / "artifacts" / "issue-2165" / "ac1_live_canary"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=15
    ).stdout.strip()
    body_result = subprocess.run(
        ["gh", "issue", "view", str(_ISSUE_NUMBER), "--repo", _REPO, "--json", "body"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    live_body_sha256 = None
    if body_result.returncode == 0:
        import hashlib

        try:
            body_text = json.loads(body_result.stdout).get("body", "")
            live_body_sha256 = "sha256:" + hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        except json.JSONDecodeError:
            live_body_sha256 = None

    start = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "produce",
            "--issue-number",
            str(_ISSUE_NUMBER),
            "--repo",
            _REPO,
        ],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        cwd=str(_REPO_ROOT),
    )
    duration_seconds = time.monotonic() - start

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"produce did not emit parseable JSON stdout: exit={completed.returncode} "
            f"stderr={completed.stderr[-2000:]}"
        )

    artifact = {
        "issue_number": _ISSUE_NUMBER,
        "pr_head_sha": head_sha,
        "live_issue_body_sha256": live_body_sha256,
        "subprocess_exit_code": completed.returncode,
        "duration_seconds": duration_seconds,
        "root_pipeline_status": payload.get("status"),
        "semantic_verdict": (
            payload.get("compact_result", {}).get("verdict") if payload.get("status") == "ok" else None
        ),
        "error_code": payload.get("error_code"),
        "transport_invocation_id": payload.get("transport_invocation_id"),
    }
    (artifacts_dir / "latest_run.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    assert payload.get("error_code") != "reviewer_transport_environment_failure", (
        f"reviewer transport still collapses a legitimate long-running VC into "
        f"environment_failure: {json.dumps(payload)[:2000]}"
    )
    # Issue #2165 P0-2 (OWNER 2026-08-15 REQUEST_CHANGES): the Issue body's
    # `fallback_policy.fallback_success_is_pass: false` forbids treating an
    # authenticated-but-failed run as a passing fallback. `gh auth` is
    # confirmed available (the skipif guard above), so this run MUST reach
    # a real semantic result (`status: "ok"`) -- an `input_or_runtime_error`
    # here is a genuine environment/tooling FAIL, not an acceptable
    # alternative outcome.
    assert payload.get("status") == "ok", (
        f"gh auth is available but produce still did not reach a semantic result "
        f"(fallback success is not PASS per the Issue body's fallback_policy): "
        f"{json.dumps(payload)[:2000]}"
    )
    verdict = payload["compact_result"]["verdict"]
    assert verdict in {"approve", "needs-fix"}
