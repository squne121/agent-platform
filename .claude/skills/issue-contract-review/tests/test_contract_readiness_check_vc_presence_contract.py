"""
test_contract_readiness_check_vc_presence_contract.py

Issue #2782 AC6:

`contract_readiness_check.py` の `--mode execute` canonical-parent
VC-presence 判定（`parent_has_vc_section` / `skip_preflight`）が、absent /
present-empty / present-nonempty / closing-hash-present の4ケースで既存の
skip 判定意図（VC section が実質的に存在しない canonical parent のみ
baseline preflight をスキップする）を維持し、presence 判定に `bool()` では
なく `extract_verification_commands_section(body) is not None` を使うことを
固定する回帰テスト（PR #2780 OWNER review comment F2 の指摘参照）。

present-empty（見出しは存在するが本文が空）は「VC section が存在しない」
のではなく「parent author が VC に opt-in したが content がまだ空」という
別の状態であり、`bool()` 判定ではこの2つが同じ False に conflate されて
しまう。本テストは、present-empty でも skip されず
`run_baseline_vc_preflight()` が呼ばれることを固定する。

Runtime Verification Applicability: not_applicable
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SKILL_DIR = _HERE.parent
_SCRIPT_PATH = _SKILL_DIR / "scripts" / "contract_readiness_check.py"

_SPEC = importlib.util.spec_from_file_location(
    "contract_readiness_check_2782_vc_presence", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_CRC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CRC)  # type: ignore[union-attr]


_MRC_PARENT = textwrap.dedent(
    """\
    ## Machine-Readable Contract

    ```yaml
    contract_schema_version: v1
    issue_kind: parent
    parent_mode: delivery-rollup
    goal_ref: "2782 execute-mode VC-presence contract regression"
    change_kind: workflow
    ```

    ## Acceptance Criteria

    - [ ] AC1: fixture canonical parent body.
    """
)


def _body_absent() -> str:
    """absent: `## Verification Commands` 見出し自体が存在しない。"""
    return _MRC_PARENT


def _body_present_empty() -> str:
    """present-empty: 見出しは存在するが本文が空。"""
    return _MRC_PARENT + textwrap.dedent(
        """\

        ## Verification Commands

        """
    )


def _body_present_nonempty() -> str:
    """present-nonempty: 見出しが存在し本文に content がある。"""
    return _MRC_PARENT + textwrap.dedent(
        """\

        ## Verification Commands

        ```bash
        # AC1
        $ echo parent-vc-check
        ```
        """
    )


def _body_closing_hash_present() -> str:
    """closing-hash-present: GFM closing-hash 見出しかつ content あり。"""
    return _MRC_PARENT + textwrap.dedent(
        """\

        ## Verification Commands ##

        ```bash
        # AC1
        $ echo parent-vc-check
        ```
        """
    )


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


def _run_execute_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: str,
) -> tuple[dict, _PreflightSpy]:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")

    spy = _PreflightSpy()
    monkeypatch.setattr(_CRC, "run_baseline_vc_preflight", spy)
    monkeypatch.setattr(
        sys, "argv", ["contract_readiness_check.py", "--body-file", str(body_file), "--mode", "execute"]
    )

    _CRC.main()

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    return result, spy


# ---------------------------------------------------------------------------
# absent: skip_preflight == True (baseline preflight は呼ばれない)
# ---------------------------------------------------------------------------


def test_absent_vc_section_skips_baseline_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result, spy = _run_execute_mode(monkeypatch, tmp_path, capsys, _body_absent())
    assert spy.calls == [], "absent VC section: baseline preflight must be skipped"
    preflight_entries = [
        sc for sc in result["source_checks"] if sc["name"] == "baseline_vc_preflight"
    ]
    assert len(preflight_entries) == 1
    assert preflight_entries[0]["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# present-empty: skip_preflight == False (baseline preflight は呼ばれる)
# ---------------------------------------------------------------------------


def test_present_empty_vc_section_does_not_skip_baseline_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """present-empty: 見出しが存在する以上、`bool()` ではなく `is not None`
    で presence を判定し、skip せず baseline preflight を呼ぶ。

    旧 `bool(extract_verification_commands_section(body))` 判定では
    "" が falsy と評価され、誤って absent と同様に skip されていた。
    """
    result, spy = _run_execute_mode(monkeypatch, tmp_path, capsys, _body_present_empty())
    assert len(spy.calls) == 1, (
        "present-empty VC section (heading present, body empty) must NOT be "
        "skipped -- presence must be judged via `is not None`, not bool()"
    )


# ---------------------------------------------------------------------------
# present-nonempty: skip_preflight == False
# ---------------------------------------------------------------------------


def test_present_nonempty_vc_section_does_not_skip_baseline_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result, spy = _run_execute_mode(monkeypatch, tmp_path, capsys, _body_present_nonempty())
    assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# closing-hash-present: skip_preflight == False
# ---------------------------------------------------------------------------


def test_closing_hash_present_vc_section_does_not_skip_baseline_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result, spy = _run_execute_mode(monkeypatch, tmp_path, capsys, _body_closing_hash_present())
    assert len(spy.calls) == 1
