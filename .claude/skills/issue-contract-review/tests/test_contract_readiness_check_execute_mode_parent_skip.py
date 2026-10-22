"""Regression coverage for #1867: contract_readiness_check.py --mode execute
canonical-parent baseline_vc_preflight() skip.

canonical parent Issue (issue_kind: parent, resolved via
resolve_existing_issue_validation_profile(body).status == "profile" and
canonical_issue_kind == "parent") does not require a
`## Verification Commands` section under existing_issue_readiness_v1. Running
baseline_vc_preflight() unconditionally in --mode execute therefore produces a
spurious VC001_NO_VERIFICATION_COMMANDS_SECTION error for such bodies. This
module verifies:

  AC6: canonical parent bodies skip run_baseline_vc_preflight() and never
       surface a VC001_NO_VERIFICATION_COMMANDS_SECTION error.
  AC7: implementation / research / unknown kind / malformed MRC / parse
       failure / label-only-parent-with-implementation-MRC bodies keep
       running run_baseline_vc_preflight() as before.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SKILL_DIR = _HERE.parent
_SCRIPT_PATH = _SKILL_DIR / "scripts" / "contract_readiness_check.py"

_SPEC = importlib.util.spec_from_file_location(
    "contract_readiness_check_1867_parent_skip", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_CRC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CRC)  # type: ignore[union-attr]


def _canonical_parent_body() -> str:
    """Canonical parent body without a `## Verification Commands` section."""
    return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
goal_ref: "1867 execute-mode parent skip regression"
change_kind: workflow
```

## Summary

Parent tracker summary for #1867 execute-mode regression coverage.

## Goal

Keep --mode execute from running baseline_vc_preflight() for canonical parent bodies.

## Desired Destination

VC001_NO_VERIFICATION_COMMANDS_SECTION must not appear for canonical parent bodies.

## Current Validated Scope

Execute-mode skip behaviour only.

## Decisions Fixed

- 2026-07-31: canonical parent bodies skip baseline_vc_preflight() in --mode execute.

## Quality Decision Record

- Status: N/A

## Parent Closure Rule

- Close after child work completes.

## Child Issues

- [ ] #1

## Remaining Parent Gaps

- [ ] none

## Phase Handoff Contract

- Parent handoff tracked here.

## Acceptance Criteria

- [ ] AC1: execute mode skips baseline_vc_preflight for canonical parent bodies.
"""


def _canonical_parent_body_with_vc_section() -> str:
    """Canonical parent body that opts into a `## Verification Commands`
    section. PR #1878 P1 review: a parent author who adds VCs must still have
    them executed and their pass/fail outcome reflected — the skip is only
    for parents WITHOUT a VC section."""
    return (
        _canonical_parent_body()
        + """
## Verification Commands

```bash
# AC1
$ echo parent-vc-check
```
"""
    )


def _implementation_body_without_vc_section() -> str:
    return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
goal_ref: "1867 execute-mode regression: implementation keeps preflight"
change_kind: bugfix
```

## Outcome

Implementation body outcome text.

## Acceptance Criteria

- [ ] AC1: preflight still runs for implementation bodies.
"""


def _research_body_without_vc_section() -> str:
    return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
goal_ref: "1867 execute-mode regression: research keeps preflight"
change_kind: workflow
```

## Acceptance Criteria

- [ ] AC1: preflight still runs for research bodies.
"""


def _unknown_kind_body() -> str:
    return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: future-kind
goal_ref: "1867 execute-mode regression: unknown kind keeps preflight"
change_kind: workflow
```

## Acceptance Criteria

- [ ] AC1: preflight still runs for unknown-kind bodies.
"""


def _malformed_mrc_body_duplicate_key() -> str:
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\nissue_kind: parent\nissue_kind: implementation\n```\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: malformed MRC keeps preflight.\n"
    )


def _parse_failure_body_no_mrc_section() -> str:
    return "## Acceptance Criteria\n\n- [ ] AC1: missing MRC section keeps preflight.\n"


def _label_only_parent_with_implementation_mrc_body() -> str:
    """resolve_existing_issue_validation_profile() only reads the MRC field, not
    labels, so a "parent-looking" label combined with an implementation MRC
    resolves identically to a plain implementation body (legacy_no_kind) and
    must keep running baseline_vc_preflight()."""
    return _implementation_body_without_vc_section()


class _PreflightSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, body: str) -> tuple[dict, int]:
        self.calls.append(body)
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "go",
                "results": [],
                "errors": [],
            },
            0,
        )


class _FailingPreflightSpy:
    """Spy simulating a failing VC (body-structure/extraction-error style
    blocked result, mapped to readiness_status "needs_fix")."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, body: str) -> tuple[dict, int]:
        self.calls.append(body)
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "blocked",
                "results": [],
                "errors": [
                    {
                        "kind": "extraction_error",
                        "message": "parent VC failed baseline execution",
                        "rule": "VC900",
                    }
                ],
            },
            1,
        )


def _run_execute_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: str,
    spy: "_PreflightSpy | _FailingPreflightSpy | None" = None,
) -> tuple[dict, _PreflightSpy]:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")

    if spy is None:
        spy = _PreflightSpy()
    monkeypatch.setattr(_CRC, "run_baseline_vc_preflight", spy)
    monkeypatch.setattr(
        sys, "argv", ["contract_readiness_check.py", "--body-file", str(body_file), "--mode", "execute"]
    )

    _CRC.main()

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    return result, spy


class TestExecuteModeCanonicalParentSkipsPreflight:
    """AC6: canonical parent bodies skip run_baseline_vc_preflight()."""

    def test_preflight_not_invoked_for_canonical_parent_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result, spy = _run_execute_mode(monkeypatch, tmp_path, capsys, _canonical_parent_body())
        assert spy.calls == [], "run_baseline_vc_preflight() must be skipped for canonical parent bodies"
        # PR #1878 P2 review: a deliberate skip now records an explicit
        # "not_applicable" baseline_vc_preflight source_checks entry (instead
        # of the entry being entirely absent) so the skip is machine-readable
        # and distinguishable from a wiring gap.
        preflight_entries = [
            source_check
            for source_check in result["source_checks"]
            if source_check["name"] == "baseline_vc_preflight"
        ]
        assert len(preflight_entries) == 1, (
            f"Expected exactly one baseline_vc_preflight source_checks entry "
            f"for a skipped canonical parent body, got: {preflight_entries}"
        )
        entry = preflight_entries[0]
        assert entry["status"] == "not_applicable", entry
        assert entry["reason_code"] == "canonical_parent_without_verification_commands", entry
        assert entry["exit_code"] is None, entry

    def test_no_vc001_error_for_canonical_parent_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result, _spy = _run_execute_mode(monkeypatch, tmp_path, capsys, _canonical_parent_body())
        assert not any(
            error.get("rule_id") == "VC001_NO_VERIFICATION_COMMANDS_SECTION" for error in result["errors"]
        ), f"Unexpected VC001 error for canonical parent body: {result['errors']}"


class TestExecuteModeCanonicalParentWithVerificationCommandsSection:
    """PR #1878 P1 review: canonical parent bodies that DO carry a
    `## Verification Commands` section must still have baseline_vc_preflight()
    invoked, and the pass/fail outcome must be reflected in build_result()'s
    output (both invocation count and resulting readiness status)."""

    def test_preflight_invoked_for_parent_with_vc_section(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result, spy = _run_execute_mode(
            monkeypatch, tmp_path, capsys, _canonical_parent_body_with_vc_section()
        )
        assert len(spy.calls) == 1, (
            "run_baseline_vc_preflight() must be invoked once for a canonical parent "
            "body carrying a `## Verification Commands` section"
        )
        assert any(
            source_check["name"] == "baseline_vc_preflight" for source_check in result["source_checks"]
        )

    def test_passing_vc_yields_go_status_for_parent_with_vc_section(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        passing_spy = _PreflightSpy()
        result, spy = _run_execute_mode(
            monkeypatch,
            tmp_path,
            capsys,
            _canonical_parent_body_with_vc_section(),
            spy=passing_spy,
        )
        assert len(spy.calls) == 1
        assert result["status"] == "go", (
            f"Expected 'go' status for passing VC on canonical parent body, got: {result}"
        )

    def test_failing_vc_yields_non_go_status_for_parent_with_vc_section(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failing_spy = _FailingPreflightSpy()
        result, spy = _run_execute_mode(
            monkeypatch,
            tmp_path,
            capsys,
            _canonical_parent_body_with_vc_section(),
            spy=failing_spy,
        )
        assert len(spy.calls) == 1
        assert result["status"] != "go", (
            f"Expected non-'go' status for failing VC on canonical parent body, got: {result}"
        )
        assert any(
            source_check["name"] == "baseline_vc_preflight" for source_check in result["source_checks"]
        )


@pytest.mark.parametrize(
    ("case_id", "body_factory"),
    [
        ("implementation", _implementation_body_without_vc_section),
        ("research", _research_body_without_vc_section),
        ("unknown_kind", _unknown_kind_body),
        ("malformed_mrc_duplicate_key", _malformed_mrc_body_duplicate_key),
        ("parse_failure_no_mrc_section", _parse_failure_body_no_mrc_section),
        ("label_only_parent_with_implementation_mrc", _label_only_parent_with_implementation_mrc_body),
    ],
)
class TestExecuteModeNonCanonicalParentKeepsPreflight:
    """AC7: implementation / research / unknown kind / malformed MRC / parse failure /
    label-only-parent-with-implementation-MRC bodies keep running
    run_baseline_vc_preflight() as before."""

    def test_preflight_invoked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        case_id: str,
        body_factory,
    ) -> None:
        body = body_factory()
        _result, spy = _run_execute_mode(monkeypatch, tmp_path, capsys, body)
        assert len(spy.calls) == 1, (
            f"[{case_id}] Expected run_baseline_vc_preflight() to be invoked exactly once, "
            f"got {len(spy.calls)} calls"
        )
        assert spy.calls[0] == body
