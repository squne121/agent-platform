"""Issue #2897 AC1 (readiness producer side): `contract_readiness_check.py`
carries the identity / applied-budget information a REAL
`baseline_vc_preflight/v1` result item already holds into
`errors[].source_payload`, and the canonical VC plan digest plus the
pre-filter `results` count into the readiness result's top level.

Production path exercised (no hand-built `timeout_provenance`):

    fake `baseline_vc_preflight.run_command()` return value (permitted seam 1:
    the only fake is the end-of-line execution boundary, using the existing
    timeout sentinel `exit_code == -1` and `stderr == "timeout"`)
      -> REAL `baseline_vc_preflight.main()` result builder
      -> REAL `contract_readiness_check.run_baseline_vc_preflight()`
      -> REAL `contract_readiness_check.main()` conversion

Permitted seam 2 (process-launch mechanics only): the cooperative supervisor
that would spawn `baseline_vc_preflight.py` as a child process is replaced by
an adapter that calls that SAME script's `main()` in-process and captures its
stdout / return code, so the seam-1 `run_command()` replacement is visible to
the "child". The result builder, the conversion and the readiness `main()`
are real.
"""

from __future__ import annotations

import contextlib
import io
import json
import signal
import sys
from pathlib import Path
from unittest import mock

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import baseline_vc_preflight as bvp  # noqa: E402
import contract_readiness_check as crc  # noqa: E402

TIMEOUT_OUTCOME = (-1, "", "timeout", 1234, {})
NOT_FOUND_OUTCOME = (4, "", "ERROR: file or directory not found: x", 5, {})

_NEW_TEST_PATH = ".claude/skills/issue-contract-review/tests/test_fixture_target_not_yet_created.py"
_PYTEST_VC = f"uv run --locked pytest {_NEW_TEST_PATH}::test_target"
_PURE_VC = "test -f README.md"

# Occurrence layout (canonical `results` order, before error filtering):
#   0  pytest VC, block 1   -> not found (expected baseline fail, no error)
#   1  pytest VC, block 2   -> TIMEOUT (same command_hash and same
#                              block-relative line as occurrence 0)
#   2  pure `test -f` block -> TIMEOUT (real execution)
#   3  pure `test -f` block -> dedup replay of occurrence 2
_BODY = f"""## Verification Commands

```bash
# AC1
# baseline-expect: fail
$ {_PYTEST_VC}
```

```bash
# AC1
# baseline-expect: fail
$ {_PYTEST_VC}
```

```bash
# AC2
$ {_PURE_VC}
```

```bash
# AC2
$ {_PURE_VC}
```

## Allowed Paths

- {_NEW_TEST_PATH}
"""


class _RunCommandSeam:
    """Seam 1: replaces `baseline_vc_preflight.run_command()` only."""

    def __init__(self, outcomes_by_call: dict[int, tuple]):
        self._outcomes_by_call = outcomes_by_call
        self.calls: list[tuple[str, int]] = []

    def __call__(self, command: str, timeout_seconds: int, cwd: str):
        call_index = len(self.calls)
        self.calls.append((command, timeout_seconds))
        return self._outcomes_by_call.get(call_index, NOT_FOUND_OUTCOME)


class _InProcessBaselineLauncher:
    """Seam 2: launch mechanics only. Runs the real
    `baseline_vc_preflight.main()` in-process instead of spawning it."""

    def __init__(self):
        self.raw_payloads: list[dict] = []

    def __call__(self, argv, *, timeout_seconds, cwd=None, env=None, **_ignored):
        assert Path(argv[1]).name == "baseline_vc_preflight.py", argv
        out, err = io.StringIO(), io.StringIO()
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(sys, "argv", [argv[1], *argv[2:]]):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    returncode = bvp.main()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        self.raw_payloads.append(json.loads(out.getvalue()))
        return bvp.SupervisedSubprocessResult(returncode, out.getvalue(), err.getvalue(), False, 0.0)


def _run_readiness_main(
    monkeypatch, tmp_path: Path, body: str, outcomes_by_call: dict[int, tuple]
) -> tuple[dict, int, _RunCommandSeam, _InProcessBaselineLauncher]:
    seam = _RunCommandSeam(outcomes_by_call)
    launcher = _InProcessBaselineLauncher()
    monkeypatch.setattr(bvp, "run_command", seam)
    monkeypatch.setattr(crc, "_run_subprocess_with_cooperative_supervisor", launcher)
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    out = io.StringIO()
    with mock.patch.object(
        sys, "argv", ["contract_readiness_check.py", "--body-file", str(body_file), "--mode", "execute"]
    ):
        with contextlib.redirect_stdout(out):
            returncode = crc.main()
    return json.loads(out.getvalue()), returncode, seam, launcher


def test_readiness_passes_bounded_provenance_and_occurrence_index(monkeypatch, tmp_path):
    # Occurrence 1 times out; the pure command (occurrence 2) times out on its
    # single real execution and occurrence 3 is its dedup replay.
    readiness, returncode, seam, launcher = _run_readiness_main(
        monkeypatch,
        tmp_path,
        _BODY,
        # call 0 = occurrence 0, call 1 = occurrence 1, call 2 = occurrence 2
        {1: TIMEOUT_OUTCOME, 2: TIMEOUT_OUTCOME},
    )
    assert len(launcher.raw_payloads) == 1
    raw = launcher.raw_payloads[0]
    raw_results = raw["results"]

    assert readiness["status"] == "human_judgment"
    assert returncode == 2

    # Top level: digest and pre-filter results count come from the existing
    # preflight payload, not from a recomputation.
    assert raw["diagnostic_report"]["status"] == "complete"
    assert readiness["canonical_plan_digest"] == raw["diagnostic_report"]["canonical_plan_digest"]
    assert readiness["results_count"] == len(raw_results) == 4

    timeout_errors = [e for e in readiness["errors"] if e["category"] == "timeout"]
    # Error-list position is NOT the canonical occurrence index: occurrence 0
    # produced no error, so the first error carries index 1.
    assert [e["source_payload"]["occurrence_index"] for e in timeout_errors] == [1, 2, 3]
    assert [e["line_start"] for e in timeout_errors] == [raw_results[i]["line"] for i in (1, 2, 3)]

    for error in timeout_errors:
        payload = error["source_payload"]
        index = payload["occurrence_index"]
        raw_item = raw_results[index]
        assert payload["line_coordinate"] == "block_relative"
        assert payload["command_hash"] == raw_item["command_hash"]
        assert payload["execution_key_hash"] == raw_item["execution_key_hash"]
        assert payload["canonical_plan_digest"] == raw["diagnostic_report"]["canonical_plan_digest"]
        # The budget is the one the real result item carries -- bounded to its
        # five known fields, not a recomputation.
        assert payload["timeout_provenance"] == raw_item["timeout_provenance"]
        assert set(payload["timeout_provenance"]) == {
            "timeout_seconds",
            "cleanup_tail_seconds",
            "source",
            "estimator_version",
            "estimator_input_digest",
        }
        # No raw command text in any of the newly carried fields.
        carried = {
            key: payload[key]
            for key in (
                "occurrence_index",
                "line_coordinate",
                "execution_key_hash",
                "execution_source",
                "dedup_source_result_index",
                "timeout_provenance",
                "canonical_plan_digest",
            )
        }
        serialized = json.dumps(carried)
        assert _PYTEST_VC not in serialized and _PURE_VC not in serialized

    # The applied budget equals the timeout actually handed to run_command()
    # for that command (this is not derived from duration_ms).
    applied_for_pytest = {t for c, t in seam.calls if c == _PYTEST_VC}
    assert applied_for_pytest == {timeout_errors[0]["source_payload"]["timeout_provenance"]["timeout_seconds"]}

    # Execution source vs dedup replay are distinguished, and the replay
    # points at the canonical index of the real execution.
    assert timeout_errors[0]["source_payload"]["execution_source"] == "executed"
    assert timeout_errors[0]["source_payload"]["dedup_source_result_index"] is None
    assert timeout_errors[1]["source_payload"]["execution_source"] == "executed"
    assert timeout_errors[2]["source_payload"]["execution_source"] == "dedup_replay"
    assert timeout_errors[2]["source_payload"]["dedup_source_result_index"] == 2
    assert timeout_errors[2]["source_payload"]["execution_key_hash"] == (
        timeout_errors[1]["source_payload"]["execution_key_hash"]
    )

    # Same AC, same command, same block-relative line in two fenced blocks:
    # `line + command_hash` would collide, the canonical index does not.
    assert raw_results[0]["command_hash"] == raw_results[1]["command_hash"]
    assert raw_results[0]["line"] == raw_results[1]["line"]
    assert raw_results[0]["ac"] == raw_results[1]["ac"]
    assert raw_results[0]["execution_key_hash"] != raw_results[1]["execution_key_hash"]


def test_not_computed_diagnostic_report_yields_null_digest_without_completion(monkeypatch, tmp_path):
    # A body with no Verification Commands section makes the real preflight
    # take an early-return path whose `diagnostic_report` is `not_computed`.
    readiness, _returncode, _seam, launcher = _run_readiness_main(
        monkeypatch, tmp_path, "## Outcome\n\nno verification commands here\n", {}
    )
    raw = launcher.raw_payloads[0]
    assert raw["diagnostic_report"]["status"] == "not_computed"
    assert readiness["canonical_plan_digest"] is None
    assert readiness["results_count"] == len(raw["results"])


def test_static_mode_readiness_result_has_no_plan_binding_keys(tmp_path):
    # Static / preflight-static modes never run the preflight, so the legacy
    # readiness shape is unchanged (no new top-level keys).
    body = tmp_path / "body.md"
    body.write_text(_BODY, encoding="utf-8")
    out = io.StringIO()
    with mock.patch.object(
        sys, "argv", ["contract_readiness_check.py", "--body-file", str(body), "--mode", "static"]
    ):
        with contextlib.redirect_stdout(out):
            crc.main()
    result = json.loads(out.getvalue())
    assert "canonical_plan_digest" not in result
    assert "results_count" not in result


@pytest.mark.parametrize(
    "bad_provenance",
    [
        None,
        {},
        {"timeout_seconds": True, "cleanup_tail_seconds": 15, "source": "static_policy",
         "estimator_version": "v2", "estimator_input_digest": "sha256:" + "0" * 64},
        {"timeout_seconds": 150, "cleanup_tail_seconds": 15, "source": "made_up_source",
         "estimator_version": "v2", "estimator_input_digest": "sha256:" + "0" * 64},
        {"timeout_seconds": 150, "cleanup_tail_seconds": 15, "source": "static_policy",
         "estimator_version": "v2", "estimator_input_digest": "not-a-digest"},
    ],
)
def test_malformed_provenance_is_dropped_not_partially_trusted(bad_provenance):
    assert crc._bounded_timeout_provenance(bad_provenance) is None


_GOOD_PROVENANCE = {
    "timeout_seconds": 150,
    "cleanup_tail_seconds": 15,
    "source": "static_policy",
    "estimator_version": "v2",
    "estimator_input_digest": "sha256:" + "0" * 64,
}


@pytest.mark.parametrize("bad", [[], {}, ["static_policy"], {"k": "v"}], ids=repr)
@pytest.mark.parametrize("field", sorted(_GOOD_PROVENANCE))
def test_unhashable_enum_like_provenance_value_is_dropped_without_exception(field, bad):
    # PR #2901 review fix_delta (P2): a list / dict value must degrade to the
    # same `None` as any other malformed value, never raise `TypeError` from
    # the frozenset membership test.
    assert crc._bounded_timeout_provenance(dict(_GOOD_PROVENANCE)) == _GOOD_PROVENANCE
    assert crc._bounded_timeout_provenance(dict(_GOOD_PROVENANCE, **{field: bad})) is None


@pytest.mark.parametrize("bad", [[], {}], ids=repr)
def test_real_conversion_with_unhashable_provenance_source_does_not_raise(
    monkeypatch, tmp_path, bad
):
    # Through the REAL preflight result builder and the REAL readiness
    # conversion: only the (permitted seam 2) child payload is degraded.
    launcher_calls = []
    monkeypatch.setattr(bvp, "run_command", lambda command, timeout_seconds, cwd: TIMEOUT_OUTCOME)

    def launcher(argv, *, timeout_seconds, cwd=None, env=None, **_ignored):
        out, err = io.StringIO(), io.StringIO()
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(sys, "argv", [argv[1], *argv[2:]]):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    returncode = bvp.main()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        payload = json.loads(out.getvalue())
        for item in payload["results"]:
            if isinstance(item.get("timeout_provenance"), dict):
                item["timeout_provenance"]["source"] = bad
        launcher_calls.append(payload)
        return bvp.SupervisedSubprocessResult(returncode, json.dumps(payload), err.getvalue(), False, 0.0)

    monkeypatch.setattr(crc, "_run_subprocess_with_cooperative_supervisor", launcher)
    body_file = tmp_path / "body.md"
    body_file.write_text(_BODY, encoding="utf-8")
    out = io.StringIO()
    with mock.patch.object(
        sys, "argv", ["contract_readiness_check.py", "--body-file", str(body_file), "--mode", "execute"]
    ):
        with contextlib.redirect_stdout(out):
            crc.main()
    result = json.loads(out.getvalue())
    assert launcher_calls
    timeout_errors = [e for e in result["errors"] if e.get("category") == "timeout"]
    assert timeout_errors
    for error in timeout_errors:
        assert error["source_payload"]["timeout_provenance"] is None
