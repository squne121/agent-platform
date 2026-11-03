"""
tests/test_run_contract_review_once.py

Unit tests for run_contract_review_once.py

AC6: run_contract_review_once.py の unit test が PASS する

B1: run_once() から check_blockers.sh / check_product_spec_contract.py /
    baseline_vc_preflight.py が全て呼ばれる（不正時は blocked/human_judgment を返す）
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once
classify_http_error = _rcr_mod.classify_http_error
HTTP_ERROR_CLASSIFICATIONS = _rcr_mod.HTTP_ERROR_CLASSIFICATIONS

_ISSUE_NUMBER = 817
_REPO = "squne121/agent-platform"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

# Issue #1914 P0-3: run_once() now fetches the Issue body exactly once, at
# the very start of every invocation, before Step 1's idempotency check even
# runs (see run_contract_review_once.py module docstring). None of the
# existing tests in this file exercise body content directly (they intercept
# _run_script's parsed JSON output), so a single generic default body is
# supplied here for every test in this file. Tests that need to control the
# fetch (e.g. a fetch failure) patch fetch_body_from_github themselves
# inside their own `with` block, which takes precedence over this default
# fixture for the duration of that block.
_DEFAULT_BODY_SNAPSHOT = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: implementation\n"
    'parent_issue: "none"\n'
    "```\n\n"
    "## Outcome\n\nfixture body for run_contract_review_once unit tests.\n"
)


@pytest.fixture(autouse=True)
def _default_body_snapshot_fetch():
    with patch.object(
        _rcr_mod, "fetch_body_from_github", return_value=(_DEFAULT_BODY_SNAPSHOT, None)
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readiness_json(status: str) -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": status,
        "body_sha256": "sha256:abc",
        "source_checks": [],
        "errors": [],
        "minimal_context": [],
        "fix_hint": None,
    }


def _make_product_spec_json(decision: str, applicability: str = "applicable") -> dict:
    return {
        "schema": "product_spec_check/v1",
        "applicability": applicability,
        "decision": decision,
        "triggers": {},
        "conditions": {},
        "blocked_reasons": [],
        "body_sha256": "sha256:abc",
        "source_provenance": {
            "source_type": "github_issue_body",
            "body_file": None,
        },
    }


def _make_vc_preflight_json(status: str) -> dict:
    return {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "status": status,
        "results": [],
        "errors": [],
    }


def _make_current_head_vc_preflight_json(status: str = "pass") -> dict:
    """Return a producer-certified current-head envelope for caller tests."""
    head = "a" * 40
    return {
        "schema": "baseline_vc_preflight/v1",
        "generated_at": "2026-07-12T00:00:00Z",
        "status": status,
        "errors": [],
        "source": {"kind": "body_file", "body_sha256": "sha256:" + "b" * 64},
        "results": [],
        "evidence_mode": "current-head",
        "head_sha": head,
        "reviewed_head_sha": head,
        "head_after_sha": head,
        "clean_before": True,
        "clean_after": True,
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
    }


def _make_declared_path_overlap_result(disjoint: bool = True) -> dict:
    """declared_path_overlap（advisory のみ、Issue #1680）の固定 stub 結果。"""
    overlapping_prs = [] if disjoint else [
        {
            "pr_number": 9999,
            "url": "https://github.com/squne121/agent-platform/pull/9999",
            "head_ref_oid": "c" * 40,
            "is_draft": False,
            "is_cross_repository": False,
            "matched_files": [".claude/skills/issue-contract-review/SKILL.md"],
        }
    ]
    return {
        "schema": "declared_path_overlap/v1",
        "advisory": True,
        "blocking": False,
        "decision": "advisory_only",
        "disjoint": disjoint,
        "overlapping_prs": overlapping_prs,
        "inventory": {
            "schema": "OPEN_PR_INVENTORY_V1",
            "totalCount": len(overlapping_prs),
            "fetched_count": len(overlapping_prs),
            "has_next_page": False,
            "complete": True,
            "saturated": False,
        },
        "errors": [],
        "note": "changed-file 名の単純な重なりのみを証明する advisory check。",
    }


def _make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


def _make_all_pass_side_effects():
    """
    Return side_effect iterables for _run_script and _run_shell_script
    that simulate all checks passing.

    _run_script call order:
      1. contract_readiness_check.py → go
      2. check_product_spec_contract.py → pass
      3. baseline_vc_preflight.py → pass

    _run_shell_script call order:
      1. check_blockers.sh → exit 0
    """
    readiness_json = _make_readiness_json("go")
    product_spec_json = _make_product_spec_json("pass", "applicable")
    vc_json = _make_vc_preflight_json("pass")

    run_script_results = [
        (readiness_json, 0, None),   # readiness
        (product_spec_json, 0, None),  # product_spec
        (vc_json, 0, None),           # vc_preflight
    ]
    shell_script_results = [
        (0, "OK: no blockers", ""),  # check_blockers.sh
    ]
    return run_script_results, shell_script_results


# ---------------------------------------------------------------------------
# B1: all four checks are called
# ---------------------------------------------------------------------------


class TestAllChecksCalledB1:
    """B1: run_once calls readiness, blockers, product_spec, and vc_preflight."""

    def test_all_four_checks_called_on_go(self, monkeypatch):
        """When all checks pass, all four are invoked and status is go."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["readiness"] == "go"
        assert result["checks"]["blockers"] == "pass"
        assert result["checks"]["product_spec"] == "pass"
        assert result["checks"]["product_spec_check"] == _make_product_spec_json(
            "pass", "applicable"
        )
        assert result["checks"]["vc_preflight"] == "pass"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is True
        assert result["checks"]["declared_path_overlap"]["advisory"] is True
        assert result["checks"]["declared_path_overlap"]["blocking"] is False


    def test_current_head_arguments_are_forwarded_to_producer(self):
        """GIVEN certified current-head input WHEN review runs THEN it preserves the full envelope."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_script_results[-1] = (_make_current_head_vc_preflight_json(), 0, None)
        captured = []

        def run_script(*args, **kwargs):
            captured.append(args[0])
            return run_script_results.pop(0)

        with patch.object(_rcr_mod, "_run_script", side_effect=run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_results[0]):
                with patch.object(
                    _rcr_mod,
                    "_run_declared_path_overlap_check",
                    return_value=_make_declared_path_overlap_result(disjoint=True),
                ):
                    result = run_once(
                        _ISSUE_NUMBER, _REPO, skip_idempotency_check=True,
                        evidence_mode="current-head", cwd="/tmp/pr-worktree", reviewed_head_sha="a" * 40,
                    )

        producer_command = captured[-1]
        assert result["status"] == "go"
        assert producer_command[-8:] == [
            "--cwd", "/tmp/pr-worktree", "--evidence-mode", "current-head",
            "--reviewed-head-sha", "a" * 40, "--format", "json",
        ]
        assert result["current_vc_result"] == _make_current_head_vc_preflight_json()
        assert result["vc_evidence"]["schema"] == "baseline_vc_preflight/v1"
        assert result["vc_evidence"]["source"]["body_sha256"].startswith("sha256:")

    def test_current_head_rejects_malformed_pass_envelope(self):
        """GIVEN malformed current-head PASS WHEN review runs THEN caller blocks it."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        malformed = _make_current_head_vc_preflight_json()
        malformed["schema"] = "wrong/v1"
        malformed.pop("results")
        run_script_results[-1] = (malformed, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=run_script_results):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_results[0]):
                result = run_once(
                    _ISSUE_NUMBER,
                    _REPO,
                    skip_idempotency_check=True,
                    evidence_mode="current-head",
                    cwd="/tmp/pr-worktree",
                    reviewed_head_sha="a" * 40,
                )

        assert result["status"] == "blocked"
        assert result["checks"]["vc_preflight"] == "blocked"
        assert result["current_vc_result"] == malformed
        assert any(error.startswith("uncertified_current_head_vc_evidence:") for error in result["errors"])

    def test_blockers_blocked_stops_pipeline(self, monkeypatch):
        """If check_blockers.sh returns exit 1 (open blockers), status: blocked."""
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(
                _rcr_mod, "_run_shell_script",
                return_value=(1, "", "human_escalation: blocker open"),
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "check_blockers"
        assert result["checks"]["blockers"] == "blocked"

    def test_blockers_human_judgment(self, monkeypatch):
        """check_blockers.sh returns 'human_escalation: native API unavailable' → human_judgment."""
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(
                _rcr_mod, "_run_shell_script",
                return_value=(1, "", "human_escalation: native dependency API unavailable"),
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "check_blockers"
        assert result["checks"]["blockers"] == "human_judgment"

    def test_product_spec_fail_blocked(self, monkeypatch):
        """check_product_spec_contract.py applicable+fail → blocked."""
        readiness_json = _make_readiness_json("go")
        product_spec_fail = _make_product_spec_json("fail", "applicable")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_fail, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "product_spec_check"
        assert result["checks"]["product_spec"] == "fail"

    def test_product_spec_human_judgment(self, monkeypatch):
        """check_product_spec_contract.py applicable+human_judgment → human_judgment."""
        readiness_json = _make_readiness_json("go")
        product_spec_hj = _make_product_spec_json("human_judgment", "applicable")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_hj, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "product_spec_check"
        assert result["checks"]["product_spec"] == "human_judgment"

    def test_product_spec_not_applicable_treated_as_pass(self, monkeypatch):
        """check_product_spec_contract.py not_applicable → treated as pass, pipeline continues."""
        readiness_json = _make_readiness_json("go")
        product_spec_na = _make_product_spec_json("pass", "not_applicable")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_na, 0, None),
            (vc_json, 0, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["product_spec"] == "pass"

    def test_vc_preflight_blocked_stops(self, monkeypatch):
        """baseline_vc_preflight blocked → status: blocked."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_blocked = _make_vc_preflight_json("blocked")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_blocked, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "vc_preflight"
        assert result["checks"]["vc_preflight"] == "blocked"

    def test_vc_preflight_human_judgment(self, monkeypatch):
        """baseline_vc_preflight human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_hj = _make_vc_preflight_json("human_judgment")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_hj, 2, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "vc_preflight"


# ---------------------------------------------------------------------------
# Issue #1631: readiness timeout is independently configurable
# ---------------------------------------------------------------------------


class TestReadinessTimeout:
    """GIVEN readiness execution WHEN timeout is configured THEN only Step 2 uses it."""

    def test_default_uses_only_readiness_timeout_and_applies_only_to_readiness(self):
        run_script_results, shell_results = _make_all_pass_side_effects()
        calls = []

        def fake_run_script(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            calls.append((cmd, timeout))
            return run_script_results.pop(0)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_results[0]):
                with patch.object(
                    _rcr_mod,
                    "_run_declared_path_overlap_check",
                    return_value=_make_declared_path_overlap_result(disjoint=True),
                ):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert calls[0][1] == _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS
        assert calls[1][1] == _rcr_mod._DEFAULT_TIMEOUT
        assert calls[2][1] == _rcr_mod._VC_PREFLIGHT_TIMEOUT

    def test_override_is_forwarded_to_readiness_and_reported_on_timeout(self):
        applied_timeout_seconds = 47
        captured = []

        def timeout_readiness(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            captured.append((cmd, timeout))
            return None, -1, "timeout"

        with patch.object(_rcr_mod, "_run_script", side_effect=timeout_readiness):
            result = run_once(
                _ISSUE_NUMBER,
                _REPO,
                skip_idempotency_check=True,
                readiness_timeout_seconds=applied_timeout_seconds,
            )

        assert result["status"] == "runtime_error"
        assert captured[0][1] == applied_timeout_seconds
        assert result["errors"] == [
            "readiness_check_error: timeout (readiness_timeout_seconds=47)"
        ]

    @pytest.mark.parametrize("invalid_value", [0, -1, False, "47"])
    def test_programmatic_api_rejects_invalid_readiness_timeout_before_running(self, invalid_value):
        with patch.object(_rcr_mod, "fetch_body_from_github") as fetch_body:
            with patch.object(_rcr_mod, "_run_script") as run_script:
                result = run_once(
                    _ISSUE_NUMBER,
                    _REPO,
                    skip_idempotency_check=True,
                    readiness_timeout_seconds=invalid_value,
                )

        assert result["status"] == "runtime_error"
        assert result["errors"] == [
            "invalid_readiness_timeout_seconds: must be a positive integer"
        ]
        fetch_body.assert_not_called()
        run_script.assert_not_called()

    def test_non_timeout_readiness_error_does_not_gain_timeout_annotation(self):
        with patch.object(
            _rcr_mod,
            "_run_script",
            return_value=(None, -1, "script_not_found: readiness.py"),
        ):
            result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert result["errors"] == [
            "readiness_check_error: script_not_found: readiness.py"
        ]

    def test_cli_wires_readiness_timeout_override(self):
        with patch.object(_rcr_mod, "run_once", return_value={"status": "go"}) as mocked:
            with patch.object(
                sys,
                "argv",
                [
                    "run_contract_review_once.py",
                    "--issue-number",
                    str(_ISSUE_NUMBER),
                    "--readiness-timeout-seconds",
                    "47",
                ],
            ):
                assert _rcr_mod.main() == 0

        assert mocked.call_args.kwargs["readiness_timeout_seconds"] == 47

    @pytest.mark.parametrize("invalid_value", ["0", "-1"])
    def test_cli_rejects_nonpositive_readiness_timeout(self, invalid_value):
        with patch.object(
            sys,
            "argv",
            [
                "run_contract_review_once.py",
                "--issue-number",
                str(_ISSUE_NUMBER),
                "--readiness-timeout-seconds",
                invalid_value,
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                _rcr_mod.main()

        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# AC12 (#2040): timeout_phase / execution_time_summary
# ---------------------------------------------------------------------------


class TestTimeoutPhaseAndExecutionSummary:
    """GIVEN a subprocess step timeout WHEN run_once runs THEN it reports which
    phase timed out (親 run-once/readiness, vc-preflight, or a generic child
    command) plus a small bounded execution_time_summary, and issues no
    automatic retry. Non-timeout runs never gain these fields."""

    def test_readiness_timeout_reports_run_once_readiness_phase(self):
        def timeout_readiness(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            return None, -1, "timeout"

        with patch.object(_rcr_mod, "_run_script", side_effect=timeout_readiness):
            result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert result["timeout_phase"] == "run_once_readiness"
        assert result["execution_time_summary"]["phase"] == "run_once_readiness"
        assert (
            result["execution_time_summary"]["timeout_seconds"]
            == _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS
        )
        assert isinstance(result["execution_time_summary"]["elapsed_seconds"], float)
        assert result["execution_time_summary"]["elapsed_seconds"] >= 0.0

    def test_vc_preflight_timeout_reports_vc_preflight_phase_not_readiness_phase(self):
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
        ])

        def run_script(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            try:
                return next(run_script_iter)
            except StopIteration:
                return None, -1, "timeout"

        with patch.object(_rcr_mod, "_run_script", side_effect=run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert result["timeout_phase"] == "vc_preflight"
        assert result["timeout_phase"] != "run_once_readiness"
        assert result["execution_time_summary"]["phase"] == "vc_preflight"
        assert (
            result["execution_time_summary"]["timeout_seconds"]
            == _rcr_mod._VC_PREFLIGHT_TIMEOUT
        )

    def test_check_blockers_timeout_reports_child_command_phase(self):
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(-1, "", "timeout")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert result["timeout_phase"] == "child_command"
        assert result["execution_time_summary"]["phase"] == "child_command"
        assert (
            result["execution_time_summary"]["timeout_seconds"]
            == _rcr_mod._DEFAULT_TIMEOUT
        )

    def test_product_spec_timeout_reports_child_command_phase(self):
        readiness_json = _make_readiness_json("go")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (None, -1, "timeout"),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert result["timeout_phase"] == "child_command"
        assert result["execution_time_summary"]["phase"] == "child_command"

    def test_no_timeout_omits_timeout_fields(self):
        """Normal (non-timeout) go run never gains timeout_phase / execution_time_summary."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert "timeout_phase" not in result
        assert "execution_time_summary" not in result

    def test_readiness_timeout_does_not_trigger_automatic_retry(self):
        """No automatic retry: _run_script is invoked exactly once on readiness timeout."""
        calls = []

        def timeout_readiness(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            calls.append((cmd, timeout))
            return None, -1, "timeout"

        with patch.object(_rcr_mod, "_run_script", side_effect=timeout_readiness):
            result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Status routing tests
# ---------------------------------------------------------------------------


class TestDeclaredPathOverlapAdvisoryOnly:
    """Issue #1680: declared_path_overlap is advisory only and never blocks."""

    def test_disjoint_open_pr_continues_go(self, monkeypatch):
        """AC2: OPEN PR が存在しても changed files が Allowed Paths と disjoint なら go を継続する。"""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is True

    def test_overlapping_open_pr_does_not_block_go(self, monkeypatch):
        """AC1/AC3: OPEN PR の changed-file 名重複（非 disjoint）だけでは blocked にならない。"""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=False),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        # declared_path_overlap は advisory のみ: disjoint=False (overlap あり)
        # でも status は go のまま — 単独では blocking にしない。
        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is False
        assert result["checks"]["declared_path_overlap"]["advisory"] is True
        assert result["checks"]["declared_path_overlap"]["blocking"] is False
        assert len(result["checks"]["declared_path_overlap"]["overlapping_prs"]) == 1

    def test_declared_path_overlap_contract_violation_forced_advisory(self, monkeypatch):
        """AC3: check の advisory/blocking フラグが崩れていても呼び出し側が安全側に強制する。"""
        broken_result = _make_declared_path_overlap_result(disjoint=False)
        broken_result["advisory"] = False
        broken_result["blocking"] = True

        with patch(
            "declared_path_overlap.compute_declared_path_overlap_for_issue",
            create=True,
            return_value=broken_result,
        ):
            checked = _rcr_mod._run_declared_path_overlap_check(_ISSUE_NUMBER, _REPO)

        assert checked["advisory"] is True
        assert checked["blocking"] is False
        assert any(
            "declared_path_overlap_contract_violation_forced_advisory" in e
            for e in checked["errors"]
        )

    def test_producer_exception_degrades_to_unavailable_advisory(self, monkeypatch):
        """P0-3: an uncaught exception in the producer must not propagate out of
        _run_declared_path_overlap_check (and therefore not out of run_once(),
        which calls this before result["status"] is set to "go")."""

        def raise_boom(*args, **kwargs):
            raise RuntimeError("boom: transient gh failure")

        with patch(
            "declared_path_overlap.compute_declared_path_overlap_for_issue",
            create=True,
            side_effect=raise_boom,
        ):
            checked = _rcr_mod._run_declared_path_overlap_check(_ISSUE_NUMBER, _REPO)

        assert checked["advisory"] is True
        assert checked["blocking"] is False
        assert checked["decision"] == "unavailable"
        assert checked["disjoint"] is None
        assert any(
            "declared_path_overlap_internal_exception" in e for e in checked["errors"]
        )

    def test_producer_exception_does_not_abort_run_once(self, monkeypatch):
        """P0-3: run_once() as a whole must still reach status: go and emit its
        JSON contract even when the declared_path_overlap producer explodes."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        def raise_boom(*args, **kwargs):
            raise RuntimeError("boom")

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch(
                        "declared_path_overlap.compute_declared_path_overlap_for_issue",
                        create=True,
                        side_effect=raise_boom,
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"]["decision"] == "unavailable"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is None

    def test_base_ref_forwarded_to_producer(self, monkeypatch):
        """P1-3: the wrapper must scope the OPEN PR inventory to the same base
        branch ("main") as the Allowed Paths review, not leave it unset."""
        captured = {}

        def fake_compute(issue_number, repo, base_ref=None, **kwargs):
            captured["issue_number"] = issue_number
            captured["repo"] = repo
            captured["base_ref"] = base_ref
            return _make_declared_path_overlap_result(disjoint=True)

        with patch(
            "declared_path_overlap.compute_declared_path_overlap_for_issue",
            create=True,
            side_effect=fake_compute,
        ):
            _rcr_mod._run_declared_path_overlap_check(_ISSUE_NUMBER, _REPO)

        assert captured["base_ref"] == "main"
        assert captured["issue_number"] == _ISSUE_NUMBER
        assert captured["repo"] == _REPO


class TestStatusRouting:
    """Test that run_once correctly routes based on readiness status."""

    def test_readiness_go_returns_go(self, monkeypatch):
        """Readiness check returns go (all others also pass) → status: go."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["source"] == "all_checks_pass"

    def test_readiness_needs_fix_returns_blocked(self, monkeypatch):
        """Readiness check returns needs_fix → status: blocked (pipeline stops)."""
        readiness_json = _make_readiness_json("needs_fix")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 1, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "readiness_check"

    def test_readiness_human_judgment_returns_human_judgment(self, monkeypatch):
        """Readiness check returns human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("human_judgment")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 2, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_check"

    def test_readiness_unknown_status_returns_runtime_error(self, monkeypatch):
        """Unknown readiness status → runtime_error (not human_judgment)."""
        readiness_json = _make_readiness_json("totally_unknown_status")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 5, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("unknown_readiness_status" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# JSON parse failure → runtime_error (not human_judgment)
# ---------------------------------------------------------------------------


class TestJsonParseFailure:
    """AC design: subprocess JSON parse failure → runtime_error, NOT human_judgment."""

    def test_json_parse_failure_is_runtime_error(self, monkeypatch):
        """Corrupt JSON from readiness check → runtime_error."""

        def fake_run_script(cmd, timeout=30):
            return (None, 0, "json_parse_error: Expecting value: line 1 column 1")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error", (
            "JSON parse failure must be runtime_error, not human_judgment"
        )
        assert any("readiness_check_error" in e for e in result["errors"])

    def test_json_parse_failure_not_human_judgment(self, monkeypatch):
        """JSON parse failure must NOT produce human_judgment status."""

        def fake_run_script(cmd, timeout=30):
            return (None, 1, "json_parse_error: unexpected end")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] != "human_judgment", (
            "JSON parse failure must never produce human_judgment"
        )


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


class TestIdempotencyCheck:
    """Test that existing go comment is returned without running review."""

    def test_existing_go_deduped(self, monkeypatch):
        """If existing go comment found → return early with deduped."""
        existing_url = f"{_ISSUE_URL}#issuecomment-1001"
        existing_go = {
            "html_url": existing_url,
            "inner": {"checks": {"product_spec_check": _make_product_spec_json("pass")}},
        }

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_go, None)):
            with patch.object(
                _rcr_mod,
                "_run_declared_path_overlap_check",
                return_value=_make_declared_path_overlap_result(disjoint=True),
            ) as overlap_check:
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        assert result["status"] == "go"
        assert result["source"] == "existing_go_comment"
        assert result["checks"]["product_spec_check"]["schema"] == "product_spec_check/v1"
        assert result["go_comment_url"] == existing_url
        assert result["idempotency_check"]["deduped"] is True
        # P0-2 (#1794 PR review): declared_path_overlap is a volatile,
        # OPEN-PR-live observation and must be recomputed fresh even on the
        # existing-go reuse path, never replayed from the saved comment.
        overlap_check.assert_called_once_with(_ISSUE_NUMBER, _REPO)
        assert result["checks"]["declared_path_overlap"]["disjoint"] is True

    def test_existing_go_deduped_recomputes_declared_path_overlap_fresh(self, monkeypatch):
        """AC (P0-2): reuse path must recompute declared_path_overlap, not replay a
        stale saved value even when the saved comment carried a different result."""
        existing_url = f"{_ISSUE_URL}#issuecomment-1002"
        stale_overlap = _make_declared_path_overlap_result(disjoint=True)
        existing_go = {
            "html_url": existing_url,
            "inner": {
                "checks": {
                    "product_spec_check": _make_product_spec_json("pass"),
                    "declared_path_overlap": stale_overlap,
                }
            },
        }
        fresh_overlap = _make_declared_path_overlap_result(disjoint=False)

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_go, None)):
            with patch.object(
                _rcr_mod, "_run_declared_path_overlap_check", return_value=fresh_overlap
            ) as overlap_check:
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        overlap_check.assert_called_once_with(_ISSUE_NUMBER, _REPO)
        # status stays go: declared_path_overlap is advisory only and never
        # blocks, even when the freshly recomputed value shows an overlap.
        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"] == fresh_overlap
        assert result["checks"]["declared_path_overlap"]["disjoint"] is False

    def test_idempotency_check_error_non_fatal(self, monkeypatch):
        """Idempotency check error → non-fatal, continue with review."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, "gh_timeout")):
            with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
                with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        # Error recorded but not fatal
        assert any("idempotency_check_error" in e for e in result["errors"])
        assert result["status"] == "go"  # Review still ran


# ---------------------------------------------------------------------------
# HTTP error classification
# ---------------------------------------------------------------------------


class TestHttpErrorClassification:
    """403/429/422 classification for contract review API calls."""

    def test_403_permission_denied(self):
        assert classify_http_error(403) == "permission_denied"

    def test_429_rate_limited(self):
        assert classify_http_error(429) == "rate_limited"

    def test_422_validation_failed(self):
        assert classify_http_error(422) == "validation_failed_or_spam"

    def test_unknown_ambiguous(self):
        assert classify_http_error(500) == "ambiguous_no_retry"
        assert classify_http_error(503) == "ambiguous_no_retry"

    def test_classification_table_complete(self):
        """Ensure all critical error codes are mapped."""
        assert 403 in HTTP_ERROR_CLASSIFICATIONS
        assert 429 in HTTP_ERROR_CLASSIFICATIONS
        assert 422 in HTTP_ERROR_CLASSIFICATIONS


# ---------------------------------------------------------------------------
# run_script helper
# ---------------------------------------------------------------------------


class TestRunScriptHelper:
    """Tests for _run_script error handling."""

    def test_timeout_returns_error(self, monkeypatch):
        """Timeout → error code, not human_judgment."""

        def fake_run_script(cmd, timeout=30):
            return (None, -1, "timeout")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("timeout" in e for e in result["errors"])

    def test_no_output_returns_runtime_error(self, monkeypatch):
        """No output from readiness check → runtime_error."""

        def fake_run_script(cmd, timeout=30):
            return (None, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"


# ---------------------------------------------------------------------------
# Schema output validation
# ---------------------------------------------------------------------------


class TestSchemaOutput:
    """Ensure CONTRACT_REVIEW_ONCE_RESULT_V1 schema fields are present."""

    def test_schema_fields_present(self, monkeypatch):
        """All required fields present in output including checks (B1)."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        required_fields = [
            "schema",
            "issue_number",
            "repo",
            "mode",
            "status",
            "source",
            "go_comment_url",
            "readiness_status",
            "readiness_errors",
            "checks",
            "idempotency_check",
            "errors",
        ]
        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

        assert result["schema"] == "CONTRACT_REVIEW_ONCE_RESULT_V1"

        # B1: checks sub-fields
        assert "readiness" in result["checks"]
        assert "blockers" in result["checks"]
        assert "product_spec" in result["checks"]
        assert "vc_preflight" in result["checks"]


# ---------------------------------------------------------------------------
# Issue #3012: a trusted fingerprint-NON-ready ``go`` that is newer than a
# trusted ``blocked`` must not hide that blocked result from
# check_existing_go_comment().  These tests call the REAL
# check_existing_go_comment() and the REAL shared parser; only the external
# ``gh api --paginate`` comment listing (the GitHub read boundary) is faked.
# Timeline notation: G = fingerprint-ready trusted go, B = trusted blocked,
# P = schema-valid trusted go that is NOT fingerprint-ready.
# ---------------------------------------------------------------------------

_NR_ISSUE_BODY = "## Test Issue Body\n\n## Allowed Paths\n- tracked.txt\n"
_NR_TRUSTED = {
    "author": "squne121",
    "author_id": 63350259,
    "author_type": "User",
    "author_association": "OWNER",
}
_NR_UNTRUSTED = {
    "author": "mallory",
    "author_id": 4242,
    "author_type": "User",
    "author_association": "NONE",
}

_nr_ecs_spec = importlib.util.spec_from_file_location(
    "ensure_contract_snapshot_for_nonready_go_tests",
    _HERE.parent.parent / "impl-review-loop" / "scripts" / "ensure_contract_snapshot.py",
)
assert _nr_ecs_spec is not None and _nr_ecs_spec.loader is not None
_nr_ecs = importlib.util.module_from_spec(_nr_ecs_spec)
_nr_ecs_spec.loader.exec_module(_nr_ecs)  # type: ignore[union-attr]

_nr_parser_spec = importlib.util.spec_from_file_location(
    "contract_review_result_parser_for_nonready_go_tests",
    _SCRIPTS_DIR / "contract_review_result_parser.py",
)
assert _nr_parser_spec is not None and _nr_parser_spec.loader is not None
_nr_parser = importlib.util.module_from_spec(_nr_parser_spec)
_nr_parser_spec.loader.exec_module(_nr_parser)  # type: ignore[union-attr]


def _nr_comment(cid, created_at, body, identity=None):
    identity = identity or _NR_TRUSTED
    return {
        "id": cid,
        "html_url": f"{_ISSUE_URL}#issuecomment-{cid}",
        "created_at": created_at,
        "updated_at": created_at,
        "body": body,
        **identity,
    }


def _nr_ready_go(cid, created_at, identity=None):
    body_sha = _nr_ecs.sha256_of(_NR_ISSUE_BODY)
    fingerprint = _nr_ecs.compute_expected_contract_fingerprint(
        issue_number=_ISSUE_NUMBER,
        contract_source_id=str(cid),
        contract_body_sha256=body_sha,
        allowed_paths=["tracked.txt"],
        base_ref="main",
        base_sha_at_snapshot="a" * 40,
    )
    review_result = {
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": {
                "schema": "product_spec_check/v1",
                "applicability": "applicable",
                "decision": "pass",
                "triggers": {},
                "conditions": {},
                "blocked_reasons": [],
                "body_sha256": body_sha,
                "source_provenance": {"source_type": "github_issue_body", "body_file": None},
            },
            "vc_preflight": "pass",
        },
        "vc_preflight_classifications": [{"ac": "AC1", "decision": "pass"}],
    }
    body = _nr_ecs._build_contract_review_comment(
        issue_number=_ISSUE_NUMBER,
        repo=_REPO,
        review_result=review_result,
        idempotency_marker="<!-- marker -->",
        body_sha256=body_sha,
        expected_contract_fingerprint=fingerprint,
    )
    return _nr_comment(cid, created_at, body, identity)


def _nr_blocked(cid, created_at, identity=None):
    body = (
        "```yaml\nCONTRACT_REVIEW_RESULT_V1:\n  status: blocked\n"
        '  generated_at: "2026-06-13T10:00:00Z"\n  generated_by: issue-contract-review\n'
        f"  issue_url: {_ISSUE_URL}\n```\n"
    )
    return _nr_comment(cid, created_at, body, identity)


def _nr_nonready_go(cid, created_at, identity=None):
    body = (
        "```yaml\nCONTRACT_REVIEW_RESULT_V1:\n  status: go\n"
        '  generated_at: "2026-06-13T11:00:00Z"\n  generated_by: issue-contract-review\n'
        f"  issue_url: {_ISSUE_URL}\n"
        f'  body_sha256: "{_nr_ecs.sha256_of(_NR_ISSUE_BODY)}"\n```\n'
    )
    return _nr_comment(cid, created_at, body, identity)


def _nr_parsed(comments):
    return _nr_parser.parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)


def _nr_assert_fixture_shape(comments, *, go_id=None, blocked_id=None, nonready_id=None):
    """Assert, after passing through the real parser, that G/B/P are what they claim to be."""
    by_id = {r["comment_id"]: r for r in _nr_parsed(comments)}
    for cid in (go_id, blocked_id, nonready_id):
        if cid is not None:
            assert by_id[cid]["is_trusted_author"] is True
    if go_id is not None:
        assert by_id[go_id]["status"] == "go"
        assert by_id[go_id]["is_fingerprint_ready"] is True
    if blocked_id is not None:
        assert by_id[blocked_id]["status"] == "blocked"
    if nonready_id is not None:
        assert by_id[nonready_id]["status"] == "go"
        assert by_id[nonready_id]["is_fingerprint_ready"] is False


def _nr_check(monkeypatch, comments, *, body=_NR_ISSUE_BODY):
    def fake_run(command, *args, **kwargs):
        assert command[:3] == ["gh", "api", "--paginate"], command
        return subprocess.CompletedProcess(
            command, 0, stdout="\n".join(json.dumps(c) for c in comments), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return _rcr_mod.check_existing_go_comment(
        _ISSUE_NUMBER, _REPO, current_body_sha256=_rcr_mod.sha256_of(body)
    )


_NR_T = "2026-06-13T08:00:0{}Z"


class TestNonreadyGoAfterBlocked:
    """Issue #3012 AC1/AC5 for run_contract_review_once.check_existing_go_comment()."""

    def test_nonready_go_after_blocked_does_not_resurrect_older_ready_go(self, monkeypatch):
        comments = [
            _nr_ready_go(101, _NR_T.format(1)),
            _nr_blocked(102, _NR_T.format(2)),
            _nr_nonready_go(103, _NR_T.format(3)),
        ]
        _nr_assert_fixture_shape(comments, go_id=101, blocked_id=102, nonready_id=103)
        assert _nr_check(monkeypatch, comments) == (None, None)

    def test_nonready_go_after_blocked_positive_control_ready_go_alone_is_returned(self, monkeypatch):
        comments = [_nr_ready_go(101, _NR_T.format(1))]
        _nr_assert_fixture_shape(comments, go_id=101)
        go, err = _nr_check(monkeypatch, comments)
        assert err is None
        assert go is not None and go["comment_id"] == 101

    def test_nonready_go_after_blocked_positive_control_nonready_go_alone_does_not_hide_ready_go(
        self, monkeypatch
    ):
        comments = [_nr_ready_go(101, _NR_T.format(1)), _nr_nonready_go(103, _NR_T.format(3))]
        _nr_assert_fixture_shape(comments, go_id=101, nonready_id=103)
        go, err = _nr_check(monkeypatch, comments)
        assert err is None
        assert go is not None and go["comment_id"] == 101

    def test_nonready_go_after_blocked_positive_control_stale_body_binding_still_rejects(
        self, monkeypatch
    ):
        comments = [_nr_ready_go(101, _NR_T.format(1))]
        assert _nr_check(monkeypatch, comments, body=_NR_ISSUE_BODY + "edited\n") == (None, None)

    def test_nonready_go_after_blocked_blocked_then_ready_go_prefers_later_ready_go(self, monkeypatch):
        comments = [
            _nr_ready_go(101, _NR_T.format(1)),
            _nr_blocked(102, _NR_T.format(2)),
            _nr_ready_go(104, _NR_T.format(4)),
        ]
        _nr_assert_fixture_shape(comments, go_id=104, blocked_id=102)
        go, err = _nr_check(monkeypatch, comments)
        assert err is None
        assert go is not None and go["comment_id"] == 104

    def test_nonready_go_after_blocked_blocked_without_older_go_stays_none(self, monkeypatch):
        comments = [_nr_blocked(102, _NR_T.format(2)), _nr_nonready_go(103, _NR_T.format(3))]
        _nr_assert_fixture_shape(comments, blocked_id=102, nonready_id=103)
        assert _nr_check(monkeypatch, comments) == (None, None)

    def test_nonready_go_after_blocked_blocked_directly_after_ready_go_rejects(self, monkeypatch):
        comments = [_nr_ready_go(101, _NR_T.format(1)), _nr_blocked(102, _NR_T.format(2))]
        assert _nr_check(monkeypatch, comments) == (None, None)

    def test_nonready_go_after_blocked_untrusted_blocked_and_plain_comments_do_not_reject(
        self, monkeypatch
    ):
        comments = [
            _nr_ready_go(101, _NR_T.format(1)),
            _nr_blocked(102, _NR_T.format(2), identity=_NR_UNTRUSTED),
            _nr_comment(105, _NR_T.format(3), "ordinary discussion", identity=_NR_UNTRUSTED),
            _nr_comment(106, _NR_T.format(3), "trusted but plain comment"),
        ]
        go, err = _nr_check(monkeypatch, comments)
        assert err is None
        assert go is not None and go["comment_id"] == 101

    def test_nonready_go_after_blocked_input_order_does_not_change_result(self, monkeypatch):
        comments = [
            _nr_ready_go(101, _NR_T.format(1)),
            _nr_blocked(102, _NR_T.format(2)),
            _nr_nonready_go(103, _NR_T.format(3)),
        ]
        assert _nr_check(monkeypatch, list(reversed(comments))) == (None, None)
        assert _nr_check(monkeypatch, [comments[2], comments[0], comments[1]]) == (None, None)

    def test_nonready_go_after_blocked_same_created_at_uses_numeric_comment_id(self, monkeypatch):
        same = _NR_T.format(5)
        blocked_wins = [
            _nr_ready_go(101, _NR_T.format(1)),
            _nr_ready_go(110, same),
            _nr_blocked(120, same),
        ]
        assert _nr_check(monkeypatch, blocked_wins) == (None, None)
        go_wins = [
            _nr_ready_go(101, _NR_T.format(1)),
            _nr_blocked(9, same),
            _nr_ready_go(10, same),
        ]
        go, err = _nr_check(monkeypatch, go_wins)
        assert err is None
        assert go is not None and go["comment_id"] == 10

    def test_nonready_go_after_blocked_pending_only_comment_returns_none(self, monkeypatch):
        pending = _nr_comment(
            107,
            _NR_T.format(2),
            "```yaml\nCONTRACT_SNAPSHOT_MATERIALIZATION_PENDING_V1:\n"
            f"  issue_number: {_ISSUE_NUMBER}\n  phase: awaiting_comment_id_binding\n```\n",
        )
        assert _nr_check(monkeypatch, [pending]) == (None, None)
