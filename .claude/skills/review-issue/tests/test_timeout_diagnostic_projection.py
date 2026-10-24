"""Issue #2897 AC1/AC2 (review merge side): `check_issue_contract.py
--mode merge_readiness` projects a bounded `timeout_diagnostics`
(`TIMEOUT_DIAGNOSTICS_V1`) from a readiness `human_judgment` produced by a
command-level VC timeout, without fabricating a deterministic blocker and
without changing `failure_class` / the operator-only route.

Every test goes through the production conversion chain; no completed
`timeout_provenance` / readiness / review dict is hand-built:

    fake `baseline_vc_preflight.run_command()` return value (permitted seam 1,
    timeout sentinel `exit_code == -1` and `stderr == "timeout"`)
      -> REAL `baseline_vc_preflight.main()` result builder
      -> REAL `contract_readiness_check.main()` conversion
      -> REAL `check_issue_contract.main()` review check (`--file --json`)
      -> REAL `check_issue_contract.main()` `--mode merge_readiness`

Permitted seam 2 (process-launch mechanics only): the cooperative supervisor
that would spawn `baseline_vc_preflight.py` as a child process is replaced by
an adapter calling that SAME script's `main()` in-process.

The "unknown attribution" cases degrade an otherwise real artifact by
REMOVING or TAMPERING a binding field (a legacy / partial producer, or a
corrupted file between readiness and merge). They never inject a completed
`timeout_provenance`.
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

_REVIEW_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_CONTRACT_REVIEW_SCRIPTS = Path(__file__).resolve().parents[2] / "issue-contract-review" / "scripts"
for _path in (_REVIEW_SCRIPTS, _CONTRACT_REVIEW_SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import baseline_vc_preflight as bvp  # noqa: E402
import check_issue_contract as cic  # noqa: E402
import contract_readiness_check as crc  # noqa: E402

TIMEOUT_OUTCOME = (-1, "", "timeout", 1234, {})
NOT_FOUND_OUTCOME = (4, "", "ERROR: file or directory not found: x", 5, {})

_NEW_TEST_PATH = ".claude/skills/review-issue/tests/test_fixture_target_not_yet_created.py"
_PYTEST_VC = f"uv run --locked pytest {_NEW_TEST_PATH}::test_target"
_PURE_VC = "test -f README.md"
_OTHER_DIGEST = "sha256:" + "ab" * 32

_BODY_HEADER = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "timeout diagnostic projection fixture"
change_kind: workflow
```

## Outcome

Fixture for the timeout diagnostic projection.

## Acceptance Criteria

- [ ] AC1: the projection keeps the timed-out occurrence identity.
- [ ] AC2: the projection never attributes a timeout to another occurrence.

## Verification Commands

"""

_ALLOWED = f"""
## Allowed Paths

- {_NEW_TEST_PATH}
"""

# Two fenced blocks holding the SAME AC and the SAME command at the SAME
# block-relative line (`line + command_hash` would collide), preceded by one
# unrelated pure command so the canonical indexes are not trivially 0/1.
_TWO_BLOCK_BODY = (
    _BODY_HEADER
    + f"""```bash
# AC1
$ {_PURE_VC}
```

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
"""
    + _ALLOWED
)

# Two pure identical commands: occurrence 0 is the real execution and
# occurrence 1 is its dedup replay.
_DEDUP_BODY = (
    _BODY_HEADER
    + f"""```bash
# AC2
$ {_PURE_VC}
```

```bash
# AC2
$ {_PURE_VC}
```
"""
    + _ALLOWED
)


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
    """Seam 2: launch mechanics only. `degrade` (optional) removes / alters a
    binding field of the REAL preflight payload to model a legacy or partial
    producer; it is never used to supply a completed provenance."""

    def __init__(self, degrade=None):
        self._degrade = degrade
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
        payload = json.loads(out.getvalue())
        self.raw_payloads.append(json.loads(out.getvalue()))
        stdout_text = out.getvalue()
        if self._degrade is not None:
            self._degrade(payload)
            stdout_text = json.dumps(payload)
        return bvp.SupervisedSubprocessResult(returncode, stdout_text, err.getvalue(), False, 0.0)


def _run_main(module_main, argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", argv):
        with contextlib.redirect_stdout(out):
            try:
                code = module_main() or 0
            except SystemExit as exc:  # check_issue_contract.main() exits
                code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue()


def _readiness_result(
    monkeypatch, tmp_path: Path, body: str, outcomes_by_call: dict[int, tuple], degrade=None
) -> tuple[dict, _InProcessBaselineLauncher]:
    launcher = _InProcessBaselineLauncher(degrade)
    monkeypatch.setattr(bvp, "run_command", _RunCommandSeam(outcomes_by_call))
    monkeypatch.setattr(crc, "_run_subprocess_with_cooperative_supervisor", launcher)
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    _code, stdout = _run_main(
        crc.main, ["contract_readiness_check.py", "--body-file", str(body_file), "--mode", "execute"]
    )
    return json.loads(stdout), launcher


def _review_result(tmp_path: Path, body: str) -> dict:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    _code, stdout = _run_main(
        cic.main, ["check_issue_contract.py", "--file", str(body_file), "--json"]
    )
    return json.loads(stdout)


def _merge(tmp_path: Path, review: dict, readiness: dict) -> tuple[int, dict | None]:
    review_file = tmp_path / "review_result.json"
    readiness_file = tmp_path / "readiness_result.json"
    output_file = tmp_path / "merged_review_result.json"
    review_file.write_text(json.dumps(review), encoding="utf-8")
    readiness_file.write_text(json.dumps(readiness), encoding="utf-8")
    if output_file.exists():
        output_file.unlink()
    code, _stdout = _run_main(
        cic.main,
        [
            "check_issue_contract.py",
            "--mode", "merge_readiness",
            "--review-result-file", str(review_file),
            "--readiness-result-file", str(readiness_file),
            "--readiness-artifact-path", str(readiness_file),
            "--iteration-id", "timeout_diagnostic_projection_test",
            "--output-file", str(output_file),
        ],
    )
    merged = json.loads(output_file.read_text(encoding="utf-8")) if output_file.exists() else None
    return code, merged


def _full_chain(monkeypatch, tmp_path, body, outcomes_by_call, degrade=None, tamper_readiness=None):
    readiness, launcher = _readiness_result(monkeypatch, tmp_path, body, outcomes_by_call, degrade)
    if tamper_readiness is not None:
        tamper_readiness(readiness)
    review = _review_result(tmp_path, body)
    code, merged = _merge(tmp_path, review, readiness)
    return review, readiness, merged, code, launcher


def test_inner_timeout_preserves_bounded_identity_without_blocker(monkeypatch, tmp_path):
    # call 0 = pure command, call 1 = first pytest block, call 2 = second
    # pytest block (times out).
    review, readiness, merged, code, launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: TIMEOUT_OUTCOME}
    )
    raw = launcher.raw_payloads[0]

    # Existing semantics are unchanged: human_judgment failure_class, a
    # needs-fix verdict and the timeout string in blocking_issues; no
    # deterministic blocker is fabricated for the timeout.
    assert readiness["status"] == "human_judgment"
    assert merged is not None and code == 1
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    assert merged["verdict"] == "needs-fix"
    assert "Command exceeded timeout" in merged["blocking_issues"]
    assert merged["structured_blockers"] == review["structured_blockers"]
    assert not any(
        "timeout" in json.dumps(blocker).lower() for blocker in merged["structured_blockers"]
    )

    diagnostics = merged["timeout_diagnostics"]
    assert diagnostics["schema_version"] == "TIMEOUT_DIAGNOSTICS_V1"
    assert diagnostics["body_sha256"] == review["body_sha256"] == readiness["body_sha256"]
    assert diagnostics["canonical_plan_digest"] == raw["diagnostic_report"]["canonical_plan_digest"]
    assert diagnostics["results_count"] == len(raw["results"]) == 3
    assert diagnostics["total_timeout_occurrences"] == 1
    assert diagnostics["truncated_count"] == 0

    (occurrence,) = diagnostics["occurrences"]
    raw_item = raw["results"][2]
    assert occurrence["attribution"] == "attributed"
    assert occurrence["reason_code"] == "binding_verified"
    assert occurrence["occurrence_index"] == 2
    assert occurrence["line"] == raw_item["line"]
    assert occurrence["line_coordinate"] == "block_relative"
    assert occurrence["command_hash"] == raw_item["command_hash"]
    assert occurrence["execution_key_hash"] == raw_item["execution_key_hash"]
    assert occurrence["execution_source"] == "executed"
    assert occurrence["dedup_source_result_index"] is None
    assert occurrence["timeout_provenance"] == raw_item["timeout_provenance"]

    # Bounded: no raw command / output / environment in the diagnostic.
    serialized = json.dumps(diagnostics)
    assert _PYTEST_VC not in serialized and _PURE_VC not in serialized
    assert "runner_env_delta" not in serialized and "minimal_context" not in serialized
    assert len(serialized.encode("utf-8")) <= 16 * 1024


@pytest.mark.parametrize("timed_out_call, expected_index", [(2, 2), (1, 1)])
def test_same_hash_same_line_other_block_only_timed_out_occurrence_attributed(
    monkeypatch, tmp_path, timed_out_call, expected_index
):
    _review, _readiness, merged, _code, launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {timed_out_call: TIMEOUT_OUTCOME}
    )
    raw_results = launcher.raw_payloads[0]["results"]
    # Both pytest blocks collide on (ac, line, command_hash)...
    assert raw_results[1]["command_hash"] == raw_results[2]["command_hash"]
    assert raw_results[1]["line"] == raw_results[2]["line"]
    assert raw_results[1]["ac"] == raw_results[2]["ac"]

    # ...but only the block that really timed out is attributed, by index.
    diagnostics = merged["timeout_diagnostics"]
    assert [o["occurrence_index"] for o in diagnostics["occurrences"]] == [expected_index]
    assert diagnostics["total_timeout_occurrences"] == 1
    (occurrence,) = diagnostics["occurrences"]
    assert occurrence["attribution"] == "attributed"
    assert occurrence["execution_key_hash"] == raw_results[expected_index]["execution_key_hash"]
    other = 1 if expected_index == 2 else 2
    assert occurrence["execution_key_hash"] != raw_results[other]["execution_key_hash"]


def test_dedup_replay_is_distinguished_from_real_execution(monkeypatch, tmp_path):
    _review, _readiness, merged, _code, launcher = _full_chain(
        monkeypatch, tmp_path, _DEDUP_BODY, {0: TIMEOUT_OUTCOME}
    )
    raw_results = launcher.raw_payloads[0]["results"]
    assert raw_results[1]["runner"] == "dedup_replay"

    occurrences = merged["timeout_diagnostics"]["occurrences"]
    assert [o["occurrence_index"] for o in occurrences] == [0, 1]
    assert [o["execution_source"] for o in occurrences] == ["executed", "dedup_replay"]
    assert [o["dedup_source_result_index"] for o in occurrences] == [None, 0]
    assert all(o["attribution"] == "attributed" for o in occurrences)
    assert occurrences[0]["execution_key_hash"] == occurrences[1]["execution_key_hash"]


def _drop_provenance(payload):
    for item in payload["results"]:
        item.pop("timeout_provenance", None)


def _drop_execution_key(payload):
    for item in payload["results"]:
        item["execution_key_hash"] = None


def _not_computed_digest(payload):
    payload["diagnostic_report"] = bvp.not_computed_diagnostic_report()


def _bad_dedup_source_out_of_range(payload):
    for item in payload["results"]:
        if item.get("dedup"):
            item["dedup"]["source_result_index"] = 99


def _bad_dedup_source_wrong_target(payload):
    # A replay can only point at an earlier real execution; point it at itself.
    for index, item in enumerate(payload["results"]):
        if item.get("dedup"):
            item["dedup"]["source_result_index"] = index


def _tamper_digest(readiness):
    readiness["canonical_plan_digest"] = _OTHER_DIGEST


def _tamper_results_count(readiness):
    readiness["results_count"] = 1


_ONE_TIMEOUT = {2: TIMEOUT_OUTCOME}
_REPLAY_TIMEOUT = {0: TIMEOUT_OUTCOME}

# (id, body, outcomes, degrade-preflight, tamper-readiness, expected reason)
_UNKNOWN_CASES = [
    ("plan_digest_missing", _TWO_BLOCK_BODY, _ONE_TIMEOUT, _not_computed_digest, None,
     "plan_digest_missing"),
    ("plan_digest_mismatch", _TWO_BLOCK_BODY, _ONE_TIMEOUT, None, _tamper_digest,
     "plan_digest_mismatch"),
    ("provenance_missing", _TWO_BLOCK_BODY, _ONE_TIMEOUT, _drop_provenance, None,
     "provenance_missing"),
    ("execution_key_missing", _TWO_BLOCK_BODY, _ONE_TIMEOUT, _drop_execution_key, None,
     "execution_key_missing"),
    ("dedup_source_out_of_range", _DEDUP_BODY, _REPLAY_TIMEOUT, _bad_dedup_source_out_of_range,
     None, "dedup_binding_invalid"),
    ("dedup_source_wrong_target", _DEDUP_BODY, _REPLAY_TIMEOUT, _bad_dedup_source_wrong_target,
     None, "dedup_binding_invalid"),
    ("occurrence_index_out_of_range", _TWO_BLOCK_BODY, _ONE_TIMEOUT, None, _tamper_results_count,
     "occurrence_index_out_of_range"),
]


@pytest.mark.parametrize(
    "body, outcomes, degrade, tamper, expected_reason",
    [case[1:] for case in _UNKNOWN_CASES],
    ids=[case[0] for case in _UNKNOWN_CASES],
)
def test_mismatch_or_missing_binding_records_unknown_without_new_gate(
    monkeypatch, tmp_path, body, outcomes, degrade, tamper, expected_reason
):
    review, readiness, merged, code, _launcher = _full_chain(
        monkeypatch, tmp_path, body, outcomes, degrade=degrade, tamper_readiness=tamper
    )

    # No new gate: the merge succeeds with the very same routing facts as a
    # fully bound timeout (human_judgment failure_class, needs-fix verdict,
    # timeout string only in blocking_issues, no deterministic blocker).
    assert merged is not None and code == 1
    assert readiness["status"] == "human_judgment"
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    assert merged["verdict"] == "needs-fix"
    assert "Command exceeded timeout" in merged["blocking_issues"]
    assert merged["structured_blockers"] == review["structured_blockers"]

    diagnostics = merged["timeout_diagnostics"]
    assert diagnostics["total_timeout_occurrences"] >= 1
    occurrences = diagnostics["occurrences"]
    if expected_reason == "dedup_binding_invalid":
        # Only the replay's binding is broken: the real execution (index 0)
        # stays attributed, and the replay is not guessed onto it.
        assert [o["attribution"] for o in occurrences] == ["attributed", "unknown"]
        assert occurrences[0]["occurrence_index"] == 0
        occurrences = occurrences[1:]
    for occurrence in occurrences:
        assert occurrence["attribution"] == "unknown"
        assert occurrence["reason_code"] == expected_reason
        # Unknown never carries completed identity / budget for a guessed VC.
        assert occurrence["occurrence_index"] is None
        assert occurrence["timeout_provenance"] is None
        assert occurrence["execution_key_hash"] is None
    assert expected_reason in cic.TIMEOUT_UNKNOWN_REASON_CODES


def test_body_sha_mismatch_still_fails_closed_without_diagnostic(monkeypatch, tmp_path):
    readiness, _launcher = _readiness_result(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: TIMEOUT_OUTCOME}
    )
    review = _review_result(tmp_path, _TWO_BLOCK_BODY)
    readiness["body_sha256"] = _OTHER_DIGEST

    with pytest.raises(ValueError, match="body_sha256 mismatch"):
        cic.merge_readiness_into_review_result(
            review,
            readiness,
            readiness_artifact_path="readiness_result.json",
            iteration_id="timeout_diagnostic_projection_test",
        )
    code, merged = _merge(tmp_path, review, readiness)
    assert code == 1
    assert merged is None


def test_non_timeout_human_judgment_gets_no_timeout_diagnostics(monkeypatch, tmp_path):
    # A non-timeout inner failure (exit 1, no recognised category) is a
    # human_judgment too; it must not produce `timeout_diagnostics`.
    _review, readiness, merged, _code, _launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: (1, "", "boom", 9, {})}
    )
    assert merged is not None
    assert "timeout_diagnostics" not in merged
    assert not any(error["category"] == "timeout" for error in readiness["errors"])


def test_more_than_sixteen_timeouts_are_truncated_within_size_bound():
    # Pure projection bound check on a readiness result with 20 timeout
    # errors whose bindings are all valid (no routing involved).
    digest = "sha256:" + "11" * 32
    errors = []
    for index in range(20):
        errors.append(
            {
                "rule_id": "VCP_TIMEOUT",
                "category": "timeout",
                "source_check": "baseline_vc_preflight",
                "line_start": 3,
                "source_payload": {
                    "occurrence_index": index,
                    "command_hash": "sha256:" + "22" * 32,
                    "execution_key_hash": "sha256:" + f"{index:02x}" * 32,
                    "execution_source": "executed",
                    "dedup_source_result_index": None,
                    "canonical_plan_digest": digest,
                    "timeout_provenance": {
                        "timeout_seconds": 150,
                        "cleanup_tail_seconds": 15,
                        "source": "static_fallback",
                        "estimator_version": "v2",
                        "estimator_input_digest": "sha256:" + "33" * 32,
                    },
                },
            }
        )
    readiness = {"errors": errors, "canonical_plan_digest": digest, "results_count": 20}
    diagnostics = cic.build_timeout_diagnostics(readiness, body_sha256="sha256:" + "44" * 32)
    assert len(diagnostics["occurrences"]) == 16
    assert diagnostics["truncated_count"] == 4
    assert diagnostics["total_timeout_occurrences"] == 20
    assert len(json.dumps(diagnostics).encode("utf-8")) <= 16 * 1024


# ---------------------------------------------------------------------------
# PR #2901 OWNER review fix_delta (Issue #2897)
# ---------------------------------------------------------------------------

_STDOUT_CAP = 65_536  # reviewer_transport.STDOUT_CAP (pinned in the root test)


def _writer_bytes(result: dict) -> int:
    """Size of `result` as the root review child really writes it:
    `print(json.dumps(merged))` -> default options, trailing newline, utf-8."""
    return len((json.dumps(result) + "\n").encode("utf-8"))


def _large_review_body(last_command_padding: int = 1450) -> str:
    # 30 long pure VCs (a large `parsed_vc_commands`, hence a large review
    # result). The last one's length is the fine-tuning knob (1 byte / char).
    blocks = []
    for index in range(30):
        padding = last_command_padding if index == 29 else 1450
        blocks.append(
            f"```bash\n# AC{(index % 2) + 1}\n$ test -f d{index}/{'a' * padding}.md\n```\n\n"
        )
    return _BODY_HEADER + "".join(blocks) + _ALLOWED


_TWENTY_TIMEOUTS = {call: TIMEOUT_OUTCOME for call in range(20)}


def _without_diagnostics(merged: dict) -> dict:
    return {key: value for key, value in merged.items() if key != "timeout_diagnostics"}


def _large_chain(monkeypatch, tmp_path, last_command_padding: int = 1450):
    body = _large_review_body(last_command_padding)
    review, readiness, merged, code, _launcher = _full_chain(
        monkeypatch, tmp_path, body, _TWENTY_TIMEOUTS
    )
    assert merged is not None and code == 1
    return body, review, readiness, merged


def _assert_routing_unchanged(review: dict, merged: dict) -> None:
    assert merged["verdict"] == "needs-fix"
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    assert "Command exceeded timeout" in merged["blocking_issues"]
    assert merged["structured_blockers"] == review["structured_blockers"]
    assert merged["parsed_vc_commands"] == review["parsed_vc_commands"]


def test_whole_result_stdout_budget_shrinks_only_the_diagnostic(monkeypatch, tmp_path):
    body, review, readiness, merged = _large_chain(monkeypatch, tmp_path)
    base = _without_diagnostics(merged)
    full_diagnostics = cic.build_timeout_diagnostics(
        readiness, body_sha256=review["body_sha256"]
    )

    # (1) Without the diagnostic the real stdout fits the transport cap.
    assert _writer_bytes(base) <= _STDOUT_CAP
    # The diagnostic alone is within its own 16 KiB bound...
    assert len(json.dumps(full_diagnostics).encode("utf-8")) <= 16 * 1024
    assert len(full_diagnostics["occurrences"]) == 16
    # (2) ...but naively attaching all of it overflows the WHOLE result.
    assert _writer_bytes(dict(base, timeout_diagnostics=full_diagnostics)) > _STDOUT_CAP

    # (3) After the fix the production merge result fits.
    assert _writer_bytes(merged) <= _STDOUT_CAP
    # (4) Routing-critical content is exactly the diagnostic-free content.
    _assert_routing_unchanged(review, merged)
    assert cic.TIMEOUT_DIAGNOSTICS_STDOUT_CAP_BYTES == _STDOUT_CAP

    # (5) Only the optional diagnostic was shrunk, consistently.
    diagnostics = merged["timeout_diagnostics"]
    kept = diagnostics["occurrences"]
    assert 0 < len(kept) < 16
    assert kept == full_diagnostics["occurrences"][: len(kept)]
    assert diagnostics["total_timeout_occurrences"] == 20
    assert diagnostics["truncated_count"] == 20 - len(kept)
    assert diagnostics["body_sha256"] == review["body_sha256"]
    # Not shrunk more than necessary: one more occurrence would overflow.
    one_more = dict(diagnostics, occurrences=full_diagnostics["occurrences"][: len(kept) + 1])
    assert _writer_bytes(dict(base, timeout_diagnostics=one_more)) > _STDOUT_CAP


def test_header_only_diagnostic_boundary_then_optional_field_omitted(monkeypatch, tmp_path):
    body, review, readiness, merged = _large_chain(monkeypatch, tmp_path)
    base_size = _writer_bytes(_without_diagnostics(merged))
    diagnostics = merged["timeout_diagnostics"]
    header_only = dict(diagnostics, occurrences=[], truncated_count=20)
    overhead = _writer_bytes(dict(_without_diagnostics(merged), timeout_diagnostics=header_only)) - base_size

    # The base result grows 1 byte per padding char: leave exactly `overhead`
    # bytes (header-only just fits) and `overhead - 1` bytes (it does not).
    fits_padding = 1450 + (_STDOUT_CAP - overhead - base_size)
    _body, review_fit, _readiness, merged_fit = _large_chain(monkeypatch, tmp_path, fits_padding)
    assert _writer_bytes(_without_diagnostics(merged_fit)) == _STDOUT_CAP - overhead
    assert merged_fit["timeout_diagnostics"]["occurrences"] == []
    assert merged_fit["timeout_diagnostics"]["total_timeout_occurrences"] == 20
    assert merged_fit["timeout_diagnostics"]["truncated_count"] == 20
    assert _writer_bytes(merged_fit) == _STDOUT_CAP
    _assert_routing_unchanged(review_fit, merged_fit)

    _body, review_omit, _readiness, merged_omit = _large_chain(
        monkeypatch, tmp_path, fits_padding + 1
    )
    assert _writer_bytes(_without_diagnostics(merged_omit)) == _STDOUT_CAP - overhead + 1
    # Optional field omitted (pre-existing contract shape); the review result
    # as a whole is NOT lost / not turned into a capture_failure.
    assert "timeout_diagnostics" not in merged_omit
    assert _writer_bytes(merged_omit) <= _STDOUT_CAP
    _assert_routing_unchanged(review_omit, merged_omit)


# --- Finding B: malformed (unhashable) enum-like values -> `unknown` --------

_MALFORMED_ENUM_VALUES = [[], {}, ["static_policy"], {"k": "v"}]


def _timeout_error(readiness: dict, occurrence_index: int) -> dict:
    (error,) = [
        e
        for e in readiness["errors"]
        if e.get("category") == "timeout"
        and (e.get("source_payload") or {}).get("occurrence_index") == occurrence_index
    ]
    return error


@pytest.mark.parametrize("bad", _MALFORMED_ENUM_VALUES, ids=repr)
@pytest.mark.parametrize(
    "field_path, expected_reason",
    [
        (("timeout_provenance", "source"), "provenance_missing"),
        (("timeout_provenance", "estimator_version"), "provenance_missing"),
        (("timeout_provenance", "estimator_input_digest"), "provenance_missing"),
        (("timeout_provenance", "timeout_seconds"), "provenance_missing"),
        (("timeout_provenance", "cleanup_tail_seconds"), "provenance_missing"),
        (("timeout_provenance",), "provenance_missing"),
        (("execution_source",), "dedup_binding_invalid"),
        (("execution_key_hash",), "execution_key_missing"),
        (("canonical_plan_digest",), "plan_digest_mismatch"),
        (("occurrence_index",), "occurrence_index_out_of_range"),
    ],
    ids=lambda value: "/".join(value) if isinstance(value, tuple) else None,
)
def test_unhashable_enum_like_values_degrade_to_unknown_without_exception(
    monkeypatch, tmp_path, field_path, expected_reason, bad
):
    def tamper(readiness):
        payload = _timeout_error(readiness, 2)["source_payload"]
        target = payload
        for key in field_path[:-1]:
            target = target[key]
        target[field_path[-1]] = bad

    review, readiness, merged, code, _launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: TIMEOUT_OUTCOME}, tamper_readiness=tamper
    )
    # The merge completes with the very same routing facts.
    assert merged is not None and code == 1
    _assert_routing_unchanged(review, merged)
    (occurrence,) = merged["timeout_diagnostics"]["occurrences"]
    assert occurrence["attribution"] == "unknown"
    assert occurrence["reason_code"] == expected_reason
    assert expected_reason in cic.TIMEOUT_UNKNOWN_REASON_CODES
    assert occurrence["occurrence_index"] is None
    assert occurrence["timeout_provenance"] is None
    assert occurrence["execution_key_hash"] is None


@pytest.mark.parametrize("bad", _MALFORMED_ENUM_VALUES, ids=repr)
def test_unhashable_provenance_source_in_real_preflight_payload_degrades(
    monkeypatch, tmp_path, bad
):
    # Producer boundary, through the REAL conversion: a (legacy / corrupted)
    # preflight item with a list / dict `source` must neither raise in the
    # readiness producer nor in the review merge.
    def degrade(payload):
        for item in payload["results"]:
            if isinstance(item.get("timeout_provenance"), dict):
                item["timeout_provenance"]["source"] = bad

    review, readiness, merged, code, _launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: TIMEOUT_OUTCOME}, degrade=degrade
    )
    assert merged is not None and code == 1
    _assert_routing_unchanged(review, merged)
    (occurrence,) = merged["timeout_diagnostics"]["occurrences"]
    assert occurrence["attribution"] == "unknown"
    assert occurrence["reason_code"] == "provenance_missing"


@pytest.mark.parametrize("bad", _MALFORMED_ENUM_VALUES, ids=repr)
def test_consumer_bounded_provenance_helper_never_raises(bad):
    good = {
        "timeout_seconds": 150,
        "cleanup_tail_seconds": 15,
        "source": "static_fallback",
        "estimator_version": "v2",
        "estimator_input_digest": "sha256:" + "33" * 32,
    }
    assert cic._timeout_bounded_provenance(good) == good
    for key in good:
        assert cic._timeout_bounded_provenance(dict(good, **{key: bad})) is None


# --- Finding C: dedup replay bound to a verified source --------------------


def _two_occurrence_chain(monkeypatch, tmp_path, tamper=None, degrade=None):
    # _DEDUP_BODY: occurrence 0 real execution (timeout), 1 its dedup replay.
    review, readiness, merged, code, launcher = _full_chain(
        monkeypatch, tmp_path, _DEDUP_BODY, {0: TIMEOUT_OUTCOME},
        degrade=degrade, tamper_readiness=tamper,
    )
    assert merged is not None and code == 1
    _assert_routing_unchanged(review, merged)
    first, second, *_extra = merged["timeout_diagnostics"]["occurrences"]
    return readiness, first, second, launcher


def test_dedup_pair_positive_control_is_attributed(monkeypatch, tmp_path):
    _readiness, first, second, _launcher = _two_occurrence_chain(monkeypatch, tmp_path)
    assert (first["attribution"], first["reason_code"]) == ("attributed", "binding_verified")
    assert (second["attribution"], second["reason_code"]) == ("attributed", "binding_verified")
    assert second["dedup_source_result_index"] == 0
    assert second["timeout_provenance"] == first["timeout_provenance"]


def test_invalid_source_occurrence_does_not_lend_binding_to_its_replay(monkeypatch, tmp_path):
    def tamper(readiness):
        # Only the SOURCE occurrence's plan digest disagrees with the top level.
        _timeout_error(readiness, 0)["source_payload"]["canonical_plan_digest"] = _OTHER_DIGEST

    _readiness, first, second, _launcher = _two_occurrence_chain(monkeypatch, tmp_path, tamper)
    assert (first["attribution"], first["reason_code"]) == ("unknown", "plan_digest_mismatch")
    assert (second["attribution"], second["reason_code"]) == ("unknown", "dedup_binding_invalid")
    assert second["timeout_provenance"] is None and second["execution_key_hash"] is None


def test_source_without_provenance_in_real_payload_makes_replay_unknown(monkeypatch, tmp_path):
    def degrade(payload):
        # Real preflight payload, source item only loses its provenance.
        payload["results"][0].pop("timeout_provenance", None)

    _readiness, first, second, _launcher = _two_occurrence_chain(
        monkeypatch, tmp_path, degrade=degrade
    )
    assert (first["attribution"], first["reason_code"]) == ("unknown", "provenance_missing")
    assert (second["attribution"], second["reason_code"]) == ("unknown", "dedup_binding_invalid")


@pytest.mark.parametrize(
    "field, value",
    [
        ("timeout_seconds", 300),
        ("cleanup_tail_seconds", 77),
        ("source", "explicit_override"),
        ("estimator_version", "v999"),
        ("estimator_input_digest", "sha256:" + "ee" * 32),
    ],
)
def test_replay_with_contradicting_budget_provenance_is_unknown(monkeypatch, tmp_path, field, value):
    def tamper(readiness):
        provenance = _timeout_error(readiness, 1)["source_payload"]["timeout_provenance"]
        assert provenance[field] != value
        provenance[field] = value

    _readiness, first, second, _launcher = _two_occurrence_chain(monkeypatch, tmp_path, tamper)
    # The source stays attributed (its own binding is intact); only the
    # contradicting replay degrades -- no budget is recomputed or guessed.
    assert (first["attribution"], first["reason_code"]) == ("attributed", "binding_verified")
    assert (second["attribution"], second["reason_code"]) == ("unknown", "dedup_binding_invalid")
    assert second["timeout_provenance"] is None


def test_conflicting_duplicate_source_index_is_not_a_verified_source(monkeypatch, tmp_path):
    def tamper(readiness):
        # A second error claims index 0 as `executed` with another key.
        clone = json.loads(json.dumps(_timeout_error(readiness, 0)))
        clone["source_payload"]["execution_key_hash"] = "sha256:" + "dd" * 32
        readiness["errors"].append(clone)

    _readiness, _first, second, _launcher = _two_occurrence_chain(monkeypatch, tmp_path, tamper)
    assert (second["attribution"], second["reason_code"]) == ("unknown", "dedup_binding_invalid")
