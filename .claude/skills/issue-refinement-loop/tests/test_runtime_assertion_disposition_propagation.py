"""Issue #2852 AC10: the assertion-level disposition classification must reach
the root review input, not merely the checkers' own return values.

`run_root_review_pipeline.py produce` builds `merged_review_result` by running
the REAL chain `check_issue_contract.py --file` -> `contract_readiness_check.py
--mode execute` -> `check_issue_contract.py --mode merge_readiness`. These
tests run that real chain end to end (only the live GitHub body fetch is
replaced with a pinned fixture, the same pattern as
`test_root_review_canonical_delivery.py`) and assert that the non-blocking
carrier

- appears in the review-issue output JSON (`non_blocking_improvements`) AND in
  `merged_review_result.non_blocking_improvements`,
- is present EXACTLY ONCE in the merged result (the review side emits it
  directly and the readiness side carries the same classification; the merge
  must not double it),
- keeps the verbatim one-line-per-binding evidence (disposition, source,
  demonstrated_by, reason) unaltered by the merge,
- and never changes the verdict, blocking issues or failure_class.

The existing consumer already preserves a readiness-side carrier registered in
`_NON_BLOCKING_READINESS_ERROR_CATEGORIES`, so `run_root_review_pipeline.py`
itself is intentionally NOT modified (Issue #2852 Stop Condition); the
`check_issue_contract.py` merge identity / dedupe is what these tests pin.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from _trusted_repo import TRUSTED_REPO  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"
REVIEW_ISSUE_SCRIPT = ROOT / ".claude" / "skills" / "review-issue" / "scripts" / "check_issue_contract.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_pipeline_module():
    name = "run_root_review_pipeline_assertion_disposition_propagation"
    spec = importlib.util.spec_from_file_location(name, PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()
_REPO = TRUSTED_REPO
_CARRIER_CODE = "runtime_assertion_disposition_classification"

_SKILL = "skill-invocation-runtime-smoke"
_SKILL_A1 = "procedure_steps_executed_in_declared_order"
_SKILL_A2 = "output_contract_schema_fields_present"
_SUBAGENT = "subagent-lifecycle-causal-evidence-smoke"
_SUBAGENT_A1 = "subagent_start_stop_causal_evidence_correlated"

_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "assertion disposition propagation fixture"
change_kind: workflow
```

## Parent Issue

none

## Outcome

`.claude/skills/foo/SKILL.md` の変更に対し runtime_assertion_bindings の分類が保持される。

## Parent Goal Ref

- Goal: disposition 分類の root review input までの保持
- Desired Destination: 分類が merged_review_result に残る

## Current Validated Scope

- `.claude/skills/foo/SKILL.md` を変更する

## Remaining Parent Gaps

なし

## In Scope

- `.claude/skills/foo/SKILL.md` を変更する

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: 手順 AC が実行順を検証する <!-- runtime-verification: true -->
- [ ] AC2: 別の deterministic test が出力契約を検証する

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true

# AC2
# baseline-expect: pass
$ true
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
execution_environment:
  cli_tools:
    - python3
skip_conditions:
  - "none"
fallback_policy:
  fallback_success_is_pass: false
artifact_requirements:
  - "artifacts/out.json"
runtime_assertion_bindings:
__BINDINGS__
```

## Allowed Paths

- `.claude/skills/foo/SKILL.md`
"""


def _body(bindings: str) -> str:
    return _BODY_TEMPLATE.replace("__BINDINGS__", bindings.rstrip("\n"))


_LEGACY_BINDINGS = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n    ac: AC1\n"
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n    ac: AC1\n"
)

_REASON_NA = "scratch から SubAgent への handoff が production path に存在しない"
_REASON_COMPAT = "AC2 の deterministic test が出力契約の実証を所有する"

_MIXED_BINDINGS = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n"
    "    disposition: non_dispositive_readiness_compat\n"
    "    demonstrated_by: AC2\n"
    f'    reason: "{_REASON_COMPAT}"\n'
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
    "    disposition: not_applicable\n"
    f'    reason: "{_REASON_NA}"\n'
)

_MIXED_EXPECTED_EVIDENCE = [
    f"{_SKILL}/{_SKILL_A1}: disposition=dispositive; source=legacy_default; demonstrated_by=-; reason=-",
    f"{_SKILL}/{_SKILL_A2}: disposition=non_dispositive_readiness_compat; source=explicit; "
    f"demonstrated_by=AC2; reason={_REASON_COMPAT}",
    f"{_SUBAGENT}/{_SUBAGENT_A1}: disposition=not_applicable; source=explicit; "
    f"demonstrated_by=-; reason={_REASON_NA}",
]


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    body_sha256 = _PIPELINE.sha256_of(body)
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return body, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    args = argparse.Namespace(issue_number=issue_number, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())
    assert rc == 0, out
    assert out["status"] == "ok", out
    return out


def _review_issue_output(tmp_path: Path, body: str) -> dict:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(REVIEW_ISSUE_SCRIPT), "--file", str(body_file), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def _carrier_entries(result: dict) -> list[dict]:
    return [e for e in result.get("non_blocking_improvements") or [] if e.get("code") == _CARRIER_CODE]


def test_review_issue_output_json_carries_the_classification(tmp_path: Path):
    """GIVEN a mixed-disposition body
    WHEN the review-issue CLI runs on it
    THEN its output JSON carries the verbatim carrier and approves."""
    output = _review_issue_output(tmp_path, _body(_MIXED_BINDINGS))
    assert output["verdict"] == "approve", output["blocking_issues"]
    entries = _carrier_entries(output)
    assert len(entries) == 1
    assert entries[0]["evidence"] == _MIXED_EXPECTED_EVIDENCE
    assert entries[0]["severity"] == "advisory"


def test_merged_review_result_keeps_classification_verbatim_and_exactly_once(tmp_path: Path, monkeypatch):
    """GIVEN a mixed-disposition body run through the REAL produce chain
    WHEN merged_review_result is built
    THEN the carrier appears exactly once with verbatim evidence, equal to the review-issue output,
    and the verdict / blockers / failure_class are not affected."""
    body = _body(_MIXED_BINDINGS)
    review_output = _review_issue_output(tmp_path, body)
    out = _run_real_produce(tmp_path, monkeypatch, body=body, issue_number=2852001)

    merged = out["merged_review_result"]
    assert merged["verdict"] == "approve", merged["blocking_issues"]
    assert merged["blocking_issues"] == [] and merged["structured_blockers"] == []
    assert merged.get("failure_class") is None
    assert out["compact_result"]["verdict"] == "approve"

    entries = _carrier_entries(merged)
    assert len(entries) == 1, merged["non_blocking_improvements"]
    assert entries[0]["code"] == _CARRIER_CODE
    assert entries[0]["evidence"] == _MIXED_EXPECTED_EVIDENCE
    # identical to the review-issue-only output -> merge neither altered nor duplicated it
    assert entries[0]["evidence"] == _carrier_entries(review_output)[0]["evidence"]
    # no RUNTIMEASSERT003-coded duplicate under a different identity
    assert not [e for e in merged["non_blocking_improvements"] if e.get("code") == "RUNTIMEASSERT003"]


def test_merged_review_result_keeps_multiline_and_semicolon_reason_as_one_line(tmp_path: Path, monkeypatch):
    """GIVEN a reason with `;` and a newline
    WHEN the real produce chain runs
    THEN the merged carrier keeps one physical line per binding with the normalized reason."""
    bindings = (
        f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
        f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n    ac: AC1\n"
        f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
        "    disposition: not_applicable\n"
        '    reason: "handoff が無い; reason=偽装\\n二行目"\n'
    )
    out = _run_real_produce(tmp_path, monkeypatch, body=_body(bindings), issue_number=2852002)
    entries = _carrier_entries(out["merged_review_result"])
    assert len(entries) == 1
    evidence = entries[0]["evidence"]
    assert len(evidence) == 3 and all("\n" not in line for line in evidence)
    assert evidence[2] == (
        f"{_SUBAGENT}/{_SUBAGENT_A1}: disposition=not_applicable; source=explicit; "
        "demonstrated_by=-; reason=handoff が無い; reason=偽装 二行目"
    )


def test_pure_legacy_input_emits_no_carrier_anywhere(tmp_path: Path, monkeypatch):
    """GIVEN a pure legacy 3-field body
    WHEN the real produce chain runs
    THEN neither the review output nor merged_review_result carries a disposition carrier."""
    body = _body(_LEGACY_BINDINGS)
    review_output = _review_issue_output(tmp_path, body)
    out = _run_real_produce(tmp_path, monkeypatch, body=body, issue_number=2852003)
    assert review_output["verdict"] == "approve"
    assert _carrier_entries(review_output) == []
    assert out["merged_review_result"]["verdict"] == "approve"
    assert _carrier_entries(out["merged_review_result"]) == []


def test_needs_fix_input_emits_no_carrier_and_keeps_needs_fix(tmp_path: Path, monkeypatch):
    """GIVEN a body whose not_applicable entry sits next to a missing required assertion
    WHEN the real produce chain runs
    THEN the primary FAIL stands, no carrier decorates it, and it stays needs-fix."""
    bindings = (
        f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
        f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
        "    disposition: not_applicable\n"
        f'    reason: "{_REASON_NA}"\n'
    )
    out = _run_real_produce(tmp_path, monkeypatch, body=_body(bindings), issue_number=2852004)
    merged = out["merged_review_result"]
    assert merged["verdict"] == "needs-fix"
    assert any(_SKILL_A2 in issue for issue in merged["blocking_issues"]), merged["blocking_issues"]
    assert _carrier_entries(merged) == []
    assert out["compact_result"]["verdict"] == "needs-fix"
