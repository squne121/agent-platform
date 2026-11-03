"""
tests/test_run_contract_review_once_wrapper_wiring.py

Issue #1333 AC2 / AC3 / AC4:

AC2: run_contract_review_once.py の _VC_PREFLIGHT_TIMEOUT が、per-command
     timeout の named constant を参照した関係式(per-command timeout ×
     想定コマンド数 + overhead の named constant)として定義され、単純な
     独立引き上げ値になっていないことを固定する。

AC3: run_contract_review_once.py が baseline_vc_preflight.py を subprocess
     起動する際、--timeout-seconds を明示的な argv として渡すことを固定する。

Runtime Verification Applicability: not_applicable
named constant の値・関係・argv 構築のみを検証する軽量テストであり、
実際に長時間 VC を実行する timing test は含まない(baseline_vc_preflight.py
の subprocess 呼び出し自体は patch.object でモックする)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import module under test (既存 test_run_contract_review_once.py と同じ
# importlib ベースのロード方式に合わせる)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once

_ISSUE_NUMBER = 1333
_REPO = "squne121/agent-platform"

# Issue #1914 P0-3: see test_run_contract_review_once.py for rationale — a
# generic default body snapshot fetch is supplied for every test in this
# file, since run_once() now fetches the body once at the start of every
# invocation.
_DEFAULT_BODY_SNAPSHOT = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: implementation\n"
    'parent_issue: "none"\n'
    "```\n\n"
    "## Outcome\n\nfixture body for wrapper wiring unit tests.\n"
)


@pytest.fixture(autouse=True)
def _default_body_snapshot_fetch():
    with patch.object(
        _rcr_mod, "fetch_body_from_github", return_value=(_DEFAULT_BODY_SNAPSHOT, None)
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers (既存 test_run_contract_review_once.py の fixture 生成パターンを踏襲)
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


# ---------------------------------------------------------------------------
# AC2: _VC_PREFLIGHT_TIMEOUT is a derived relationship, not an independent literal
# ---------------------------------------------------------------------------


class TestVcPreflightTimeoutRelationshipAC2:
    """AC2: _VC_PREFLIGHT_TIMEOUT は per-command timeout との関係式で定義される。"""

    def test_per_command_timeout_matches_baseline_vc_preflight_constant(self):
        """drift 防止: _VC_PREFLIGHT_PER_COMMAND_TIMEOUT は
        baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS と同一の named
        constant を import して参照している(独立した重複定義ではない)。"""
        import baseline_vc_preflight  # noqa: E402 (sys.path はモジュールロード時に設定済み)

        assert (
            _rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT
            == baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS
        )

    def test_vc_preflight_timeout_is_derived_relationship_not_bare_literal(self):
        """_VC_PREFLIGHT_TIMEOUT = per-command timeout × budget + overhead の
        関係式で導出されており、これらの named constant から再計算した値と
        一致する(単純な独立リテラル引き上げではないことを固定する)。"""
        assert hasattr(_rcr_mod, "_VC_PREFLIGHT_MAX_COMMAND_BUDGET")
        assert hasattr(_rcr_mod, "_VC_PREFLIGHT_OVERHEAD_SECONDS")

        expected = (
            _rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT
            * _rcr_mod._VC_PREFLIGHT_MAX_COMMAND_BUDGET
            + _rcr_mod._VC_PREFLIGHT_OVERHEAD_SECONDS
        )
        assert _rcr_mod._VC_PREFLIGHT_TIMEOUT == expected

    def test_vc_preflight_timeout_safely_exceeds_measured_test_runtime(self):
        """実測 test_baseline_vc_preflight.py 実行時間(約58秒)を安全に上回る。"""
        assert _rcr_mod._VC_PREFLIGHT_TIMEOUT >= 180

    def test_source_does_not_hardcode_independent_literal(self):
        """回帰防止: `_VC_PREFLIGHT_TIMEOUT = <bare int literal>` の
        独立リテラル代入に戻っていないことをソース検査で固定する。"""
        with open(_RCR_PATH) as f:
            script_content = f.read()

        assert "_VC_PREFLIGHT_TIMEOUT = 180" not in script_content
        assert "_VC_PREFLIGHT_TIMEOUT = 600" not in script_content
        assert "_VC_PREFLIGHT_TIMEOUT = 900" not in script_content
        assert (
            "_VC_PREFLIGHT_TIMEOUT = (\n"
            "    _VC_PREFLIGHT_PER_COMMAND_TIMEOUT * _VC_PREFLIGHT_MAX_COMMAND_BUDGET\n"
            "    + _VC_PREFLIGHT_OVERHEAD_SECONDS\n"
            ")"
        ) in script_content


# ---------------------------------------------------------------------------
# Issue #1631: readiness outer timeout must exceed its nested execute timeout
# ---------------------------------------------------------------------------


class TestReadinessTimeoutRelationship:
    """The outer readiness wrapper must not preempt its nested execute budget."""

    def test_default_is_derived_from_nested_execute_budget_and_overhead(self):
        assert _rcr_mod._READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS == 120
        assert (
            _rcr_mod._READINESS_WRAPPER_OVERHEAD_SECONDS
            == _rcr_mod._DEFAULT_TIMEOUT
        )
        assert _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS == (
            _rcr_mod._READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS
            + _rcr_mod._READINESS_WRAPPER_OVERHEAD_SECONDS
        )
        assert (
            _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS
            > _rcr_mod._READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS
        )

    def test_source_uses_named_relationship_not_a_bare_readiness_literal(self):
        script_content = _RCR_PATH.read_text(encoding="utf-8")

        assert "_DEFAULT_READINESS_TIMEOUT_SECONDS = 90" not in script_content
        assert (
            "_DEFAULT_READINESS_TIMEOUT_SECONDS = (\n"
            "    _READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS\n"
            "    + _READINESS_WRAPPER_OVERHEAD_SECONDS\n"
            ")"
        ) in script_content


# ---------------------------------------------------------------------------
# AC3: --timeout-seconds is passed explicitly as argv to baseline_vc_preflight.py
# ---------------------------------------------------------------------------


class TestBaselineVcPreflightTimeoutArgvAC3:
    """AC3 (#1333) + Issue #2233 fix_delta P0-1 (OWNER merge_blocker 1):
    baseline_vc_preflight.py の subprocess 起動 argv に --timeout-seconds を
    無条件に明示することは廃止した(そうすると常に source: explicit_override
    になり、canonical plan 由来の per-command budget -- 特に static_policy
    ソースの budget -- が握りつぶされていた)。代わりに --expected-plan-digest
    が明示的に含まれ、その値が同じ body から computed した
    compute_canonical_vc_plan()['plan_digest'] と一致することを固定する。"""

    def test_timeout_seconds_argv_is_not_unconditionally_passed(self):
        """fix_delta P0-1: no operator-facing override flag exists on
        run_contract_review_once.py's own CLI, so `--timeout-seconds` MUST
        NOT be forced onto every invocation (regression guard for OWNER
        merge_blocker 1)."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_json, 0, None),
            ]
        )
        captured_calls = []

        def _fake_run_script(cmd, *args, **kwargs):
            captured_calls.append(cmd)
            return next(run_script_iter)

        with patch.object(_rcr_mod, "_run_script", side_effect=_fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod, "_run_declared_path_overlap_check", return_value={"disjoint": True}
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"

        # vc_preflight is the 3rd _run_script call (readiness, product_spec, vc_preflight)
        vc_preflight_cmd = captured_calls[2]
        assert str(_rcr_mod._BASELINE_VC_PREFLIGHT_PY) in vc_preflight_cmd
        assert "--timeout-seconds" not in vc_preflight_cmd

    def test_expected_plan_digest_argv_passed_and_matches_same_body_plan(self):
        """fix_delta P0-1: --expected-plan-digest IS explicitly passed, and
        its value is exactly `compute_canonical_vc_plan(body_snapshot)['plan_digest']`
        for the SAME `_DEFAULT_BODY_SNAPSHOT` fixture body this test module
        fetches (via the autouse `_default_body_snapshot_fetch` fixture)."""
        import baseline_vc_preflight

        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_json, 0, None),
            ]
        )
        captured_calls = []

        def _fake_run_script(cmd, *args, **kwargs):
            captured_calls.append(cmd)
            return next(run_script_iter)

        with patch.object(_rcr_mod, "_run_script", side_effect=_fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod, "_run_declared_path_overlap_check", return_value={"disjoint": True}
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"

        vc_preflight_cmd = captured_calls[2]
        assert "--expected-plan-digest" in vc_preflight_cmd
        digest_idx = vc_preflight_cmd.index("--expected-plan-digest")
        expected_plan = baseline_vc_preflight.compute_canonical_vc_plan(_DEFAULT_BODY_SNAPSHOT)
        assert vc_preflight_cmd[digest_idx + 1] == expected_plan["plan_digest"]



# ---------------------------------------------------------------------------
# Issue #1338 AC9: run_contract_review_once.py holds a named constant
# _VC_PREFLIGHT_MAX_WORKERS and passes it, together with --timeout-seconds,
# as explicit argv when invoking baseline_vc_preflight.py.
# ---------------------------------------------------------------------------


class TestVcPreflightMaxWorkersWiringAC9:
    """AC9: _VC_PREFLIGHT_MAX_WORKERS named constant + explicit argv wiring."""

    def test_named_constant_exists_with_expected_initial_value(self):
        assert hasattr(_rcr_mod, "_VC_PREFLIGHT_MAX_WORKERS")
        assert _rcr_mod._VC_PREFLIGHT_MAX_WORKERS == 2

    def test_vc_preflight_invocation_passes_explicit_max_workers_not_timeout(self):
        """The baseline_vc_preflight.py subprocess argv explicitly includes
        --max-workers (sourced from the named constant), and (fix_delta
        P0-1) does NOT include --timeout-seconds (see
        TestBaselineVcPreflightTimeoutArgvAC3 above for the digest-based
        replacement mechanism)."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_json, 0, None),
            ]
        )
        captured_calls = []

        def _fake_run_script(cmd, *args, **kwargs):
            captured_calls.append(cmd)
            return next(run_script_iter)

        with patch.object(_rcr_mod, "_run_script", side_effect=_fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod, "_run_declared_path_overlap_check", return_value={"disjoint": True}
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"

        # vc_preflight is the 3rd _run_script call (readiness, product_spec, vc_preflight)
        vc_preflight_cmd = captured_calls[2]
        assert str(_rcr_mod._BASELINE_VC_PREFLIGHT_PY) in vc_preflight_cmd

        assert "--timeout-seconds" not in vc_preflight_cmd

        assert "--max-workers" in vc_preflight_cmd
        max_workers_idx = vc_preflight_cmd.index("--max-workers")
        assert vc_preflight_cmd[max_workers_idx + 1] == str(_rcr_mod._VC_PREFLIGHT_MAX_WORKERS)
        assert vc_preflight_cmd[max_workers_idx + 1] == "2"


# ---------------------------------------------------------------------------
# AC12 (#2040): timeout_phase reuses these same named timeout constants and
# does not disturb the derived-relationship / explicit-argv wiring above.
# ---------------------------------------------------------------------------


class TestTimeoutPhaseUsesExistingNamedConstantsAC12:
    """AC12: timeout_phase reporting must reference the existing named
    timeout constants (not introduce independent duplicate literals), and
    the readiness/vc_preflight timeout relationships above must be
    unaffected by the new fields."""

    def test_readiness_timeout_reports_configured_timeout_seconds_value(self):
        def timeout_readiness(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            return None, -1, "timeout"

        with patch.object(_rcr_mod, "_run_script", side_effect=timeout_readiness):
            result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["timeout_phase"] == "run_once_readiness"
        assert (
            result["execution_time_summary"]["timeout_seconds"]
            == _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS
        )
        # unaffected: relationship still holds
        assert _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS == (
            _rcr_mod._READINESS_NESTED_EXECUTE_TIMEOUT_SECONDS
            + _rcr_mod._READINESS_WRAPPER_OVERHEAD_SECONDS
        )

    def test_vc_preflight_timeout_reports_derived_vc_preflight_timeout_value(self):
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (None, -1, "timeout"),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["timeout_phase"] == "vc_preflight"
        assert (
            result["execution_time_summary"]["timeout_seconds"]
            == _rcr_mod._VC_PREFLIGHT_TIMEOUT
        )
        # unaffected: relationship still holds
        expected = (
            _rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT
            * _rcr_mod._VC_PREFLIGHT_MAX_COMMAND_BUDGET
            + _rcr_mod._VC_PREFLIGHT_OVERHEAD_SECONDS
        )
        assert _rcr_mod._VC_PREFLIGHT_TIMEOUT == expected


# ---------------------------------------------------------------------------
# Issue #2233 AC2: command-level timeout budget plumbing across the review
# wrapper chain. Complements TestVcPreflightTimeoutRelationshipAC2 (#1333)
# above, which already fixes that `_VC_PREFLIGHT_PER_COMMAND_TIMEOUT` is a
# single import (not an independent literal); this class additionally
# fixes that `contract_readiness_check.py`'s cleanup-tail constant and
# `run_root_review_pipeline.py`'s canonical-plan consumption are likewise
# sourced from `baseline_vc_preflight.py`'s single-owner constants/producer
# -- and that the plan's NEW `command_budgets[]` field is present and
# internally consistent.
# ---------------------------------------------------------------------------


def _load_run_root_review_pipeline():
    """Load `run_root_review_pipeline.py` the same importlib way this file
    loads `run_contract_review_once.py` above, so its own
    `sys.path.insert()` side effects (needed to resolve ITS `from
    baseline_vc_preflight import ...` / `from contract_readiness_check
    import ...`) run before we inspect its module-level bindings."""
    _rrrp_path = (
        _HERE.parent.parent / "issue-refinement-loop" / "scripts" / "run_root_review_pipeline.py"
    )
    _spec = importlib.util.spec_from_file_location("run_root_review_pipeline", _rrrp_path)
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    return _mod


class TestCommandTimeoutBudgetWiringIssue2233:
    """AC2: no file independently re-specifies the per-command timeout or
    cleanup-tail scalar; all four consumers derive from the SAME canonical
    plan / named constants baseline_vc_preflight.py owns."""

    def test_contract_readiness_check_cleanup_tail_matches_baseline_constant(self):
        """`contract_readiness_check._PER_VC_SLOT_CLEANUP_TAIL_SECONDS` is
        imported from `baseline_vc_preflight.CLEANUP_TAIL_SECONDS`, not an
        independent local literal `15`."""
        import baseline_vc_preflight
        import contract_readiness_check

        assert (
            contract_readiness_check._PER_VC_SLOT_CLEANUP_TAIL_SECONDS
            == baseline_vc_preflight.CLEANUP_TAIL_SECONDS
        )

    def test_contract_readiness_check_source_does_not_hardcode_cleanup_tail_literal(self):
        """Regression guard: `_PER_VC_SLOT_CLEANUP_TAIL_SECONDS = 15` (a bare
        re-specified literal) must not reappear in the source."""
        crc_path = _SCRIPTS_DIR / "contract_readiness_check.py"
        with open(crc_path) as f:
            script_content = f.read()
        assert "_PER_VC_SLOT_CLEANUP_TAIL_SECONDS = 15" not in script_content
        assert "_PER_VC_SLOT_CLEANUP_TAIL_SECONDS = _COMMAND_CLEANUP_TAIL_SECONDS" in script_content

    def test_run_root_review_pipeline_imports_same_compute_canonical_vc_plan(self):
        """`run_root_review_pipeline.py`'s `_compute_canonical_vc_plan` IS
        (by identity) `baseline_vc_preflight.compute_canonical_vc_plan` --
        not a re-implemented or independently-derived function."""
        import baseline_vc_preflight

        run_root_review_pipeline = _load_run_root_review_pipeline()
        assert (
            run_root_review_pipeline._compute_canonical_vc_plan
            is baseline_vc_preflight.compute_canonical_vc_plan
        )

    def test_run_root_review_pipeline_imports_same_derive_review_budget(self):
        import contract_readiness_check

        run_root_review_pipeline = _load_run_root_review_pipeline()
        assert (
            run_root_review_pipeline._derive_review_budget
            is contract_readiness_check.derive_review_budget
        )

    def test_canonical_plan_command_budgets_present_and_consistent(self):
        """The canonical plan `contract_readiness_check.py` /
        `run_root_review_pipeline.py` consume now carries `command_budgets`
        (Issue #2233 AC1), and `aggregate_timeout_seconds` is the sum of
        each occurrence's own `timeout_seconds + cleanup_tail_seconds`."""
        import baseline_vc_preflight

        body = (
            "## Verification Commands\n\n"
            "```bash\n$ pnpm typecheck\n```\n\n"
            "```bash\n$ pnpm lint\n```\n"
        )
        plan = baseline_vc_preflight.compute_canonical_vc_plan(body)

        assert "command_budgets" in plan
        assert "aggregate_timeout_seconds" in plan
        assert "estimator_evidence_digest" in plan
        assert len(plan["command_budgets"]) == 2

        expected_sum = sum(
            b["timeout_seconds"] + b["cleanup_tail_seconds"] for b in plan["command_budgets"]
        )
        assert plan["aggregate_timeout_seconds"] == expected_sum


# ---------------------------------------------------------------------------
# Issue #2254 fix_delta P0 blocker 1: Step 2 (contract_readiness_check.py)
# receives the SAME --history-snapshot-file Step 5 (baseline_vc_preflight.py
# directly) receives.
# ---------------------------------------------------------------------------


class TestHistorySnapshotPropagationToStep2Blocker1:
    """Issue #2254 fix_delta P0 blocker 1 (OWNER REQUEST_CHANGES
    https://github.com/squne121/agent-platform/pull/2382#issuecomment-5458281756):
    Step 2 (contract_readiness_check.py, which in --mode execute ALSO runs
    baseline_vc_preflight.py internally via run_baseline_vc_preflight())
    must receive the SAME --history-snapshot-file Step 5
    (baseline_vc_preflight.py invoked directly) receives -- previously
    only Step 5 got it, so the two internal baseline_vc_preflight.py
    executions within ONE run_once() invocation could observe DIFFERENT
    points-in-time of the history store."""

    def test_step2_and_step5_receive_the_same_history_snapshot_file(self, tmp_path, monkeypatch):
        store_path = tmp_path / "wrapper-wiring-store.sqlite3"
        # Bypasses produce_immutable_history_snapshot()'s pytest test-safety
        # guard (PYTEST_CURRENT_TEST set + no explicit override -> None)
        # so this invocation actually produces a (here: store_status
        # "missing", since the file does not exist yet) non-None snapshot
        # dict, exercising the SAME propagation path a real invocation
        # with an existing store would.
        monkeypatch.setenv("VC_RUNTIME_HISTORY_STORE_PATH", str(store_path))

        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_json, 0, None),
            ]
        )
        captured_calls = []

        def _fake_run_script(cmd, *args, **kwargs):
            captured_calls.append(cmd)
            return next(run_script_iter)

        with patch.object(_rcr_mod, "_run_script", side_effect=_fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod, "_run_declared_path_overlap_check", return_value={"disjoint": True}
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"

        # readiness (Step 2) is the 1st _run_script call, vc_preflight
        # (Step 5) is the 3rd (readiness, product_spec, vc_preflight).
        readiness_cmd = captured_calls[0]
        vc_preflight_cmd = captured_calls[2]

        assert "--history-snapshot-file" in readiness_cmd, readiness_cmd
        assert "--history-snapshot-file" in vc_preflight_cmd, vc_preflight_cmd

        readiness_snapshot_path = readiness_cmd[
            readiness_cmd.index("--history-snapshot-file") + 1
        ]
        vc_preflight_snapshot_path = vc_preflight_cmd[
            vc_preflight_cmd.index("--history-snapshot-file") + 1
        ]
        assert readiness_snapshot_path == vc_preflight_snapshot_path
