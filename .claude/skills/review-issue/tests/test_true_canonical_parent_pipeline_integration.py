"""
test_true_canonical_parent_pipeline_integration.py

PR #1878 P2 review: the existing `_parent_body_no_outcome_no_vc()`-style
fixtures used by test_check_issue_contract_c4_parent_na.py /
test_check_issue_contract_c8_parent_na.py only carry the Machine-Readable
Contract, Summary, Goal and Acceptance Criteria sections — they do NOT model
a real `existing_issue_readiness_v1` canonical parent body, which additionally
requires Desired Destination / Current Validated Scope / Decisions Fixed /
Quality Decision Record / Parent Closure Rule / Child Issues / Remaining
Parent Gaps / Phase Handoff Contract
(`.claude/skills/create-issue/scripts/validate_issue_body.py`
`_EXISTING_ISSUE_READINESS_V1_PARENT_SECTIONS`).

This module exercises the full production 3-stage pipeline as real
subprocesses against a fixture that DOES carry every required section:

  1. `check_issue_contract.py --file <body> --json`
     -> REVIEW_ISSUE_RESULT_V1
  2. `contract_readiness_check.py --body-file <body> --mode execute`
     -> ISSUE_CONTRACT_READINESS_RESULT_V1
  3. `check_issue_contract.py --mode merge_readiness
     --review-result-file ... --readiness-result-file ...`
     -> merged REVIEW_ISSUE_RESULT_V1

Existing spy-based unit tests (test_check_issue_contract_c4_parent_na.py,
test_check_issue_contract_c8_parent_na.py,
test_contract_readiness_check_execute_mode_parent_skip.py) are left in place
unmodified; this module adds integration-level coverage on top.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REVIEW_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
CHECK_ISSUE_CONTRACT_PY = REVIEW_SCRIPTS_DIR / "check_issue_contract.py"
CONTRACT_READINESS_CHECK_PY = (
    Path(__file__).resolve().parent.parent.parent
    / "issue-contract-review"
    / "scripts"
    / "contract_readiness_check.py"
)
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Fixture bodies
# ---------------------------------------------------------------------------

_TRUE_CANONICAL_PARENT_BASE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
goal_ref: "PR #1878 true canonical parent pipeline fixture"
change_kind: workflow
```

## Summary

True canonical parent fixture for the #1867/#1878 integration test.

## Goal

Exercise the full 3-stage pipeline against a genuinely complete canonical
parent body (all existing_issue_readiness_v1 parent sections present).

## Desired Destination

All child issues complete and the parent tracker is ready to close.

## Current Validated Scope

- Section-complete canonical parent fixture validated end-to-end.

## Decisions Fixed

- 2026-07-31: this fixture models existing_issue_readiness_v1 parent sections exactly.

## Quality Decision Record

- Status: N/A (fixture-only tracker; no external quality gate applies).

## Parent Closure Rule

- Close once all child issues are complete and no remaining gaps are open.

## Child Issues

- [ ] #1 (placeholder child)

## Remaining Parent Gaps

- [ ] none

## Phase Handoff Contract

- Parent handoff is tracked entirely within this Issue.

## Acceptance Criteria

- [ ] AC1: parent readiness holds with all required sections present and no VC section.
"""

_TRUE_CANONICAL_PARENT_FAILING_VC = (
    _TRUE_CANONICAL_PARENT_BASE
    + """
## Verification Commands

```bash
# AC1
$ echo a && echo b
```
"""
)

_IMPLEMENTATION_BASE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "PR #1878 true canonical parent pipeline: implementation fixture"
change_kind: code
```

## Parent Issue

none

## Outcome

`src/pr1878_sample.ts` に PR1878Sample クラスが追加され、`pnpm test` でテストが通っている。

## Background

PR1878 integration test fixture.

## Parent Goal Ref

- Goal: PR1878 integration fixture
- Desired Destination: PR1878Sample が動作すること

## Current Validated Scope

- `src/pr1878_sample.ts` に PR1878Sample クラスを追加

## Remaining Parent Gaps

なし

## In Scope

- `src/pr1878_sample.ts` に PR1878Sample クラスを追加

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `src/pr1878_sample.ts` に `PR1878Sample` クラスが存在する

## Verification Commands

```bash
# AC1
$ grep -r "class PR1878Sample" src/pr1878_sample.ts
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: PR1878 integration test fixture only.

## Allowed Paths

- `src/pr1878_sample.ts`
"""

_IMPLEMENTATION_MISSING_OUTCOME = _IMPLEMENTATION_BASE.replace(
    """## Outcome

`src/pr1878_sample.ts` に PR1878Sample クラスが追加され、`pnpm test` でテストが通っている。

""",
    "",
)

_IMPLEMENTATION_MISSING_VC = _IMPLEMENTATION_BASE.replace(
    """## Verification Commands

```bash
# AC1
$ grep -r "class PR1878Sample" src/pr1878_sample.ts
```

""",
    "",
)

_MALFORMED_MRC_DUPLICATE_KEY = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: parent\n"
    "issue_kind: implementation\n"
    "```\n\n"
    "## Acceptance Criteria\n\n- [ ] AC1: malformed MRC duplicate key.\n"
)

_UNKNOWN_ISSUE_KIND = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: some-future-kind\n"
    "```\n\n"
    "## Acceptance Criteria\n\n- [ ] AC1: unknown issue_kind.\n"
)

_DECOY_YAML_IN_NOTES = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Outcome

Decoy body outcome text without allowed paths / VC / stop conditions.

## Acceptance Criteria

- [ ] AC1: decoy fixture missing several required implementation sections.

## Notes

Historical decoy below, not part of the MRC section:

```yaml
contract_schema_version: v1
issue_kind: parent
```
"""


# ---------------------------------------------------------------------------
# Subprocess pipeline helpers
# ---------------------------------------------------------------------------


def _write_body(tmp_path: Path, name: str, body: str) -> Path:
    body_file = tmp_path / name
    body_file.write_text(body, encoding="utf-8")
    return body_file


def _run_review(body_file: Path) -> tuple[dict, int]:
    """Stage 1: check_issue_contract.py --file <body> --json."""
    result = subprocess.run(
        [sys.executable, str(CHECK_ISSUE_CONTRACT_PY), "--file", str(body_file), "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.stdout, f"No stdout from check_issue_contract.py (stderr: {result.stderr})"
    return json.loads(result.stdout), result.returncode


def _run_readiness(body_file: Path) -> tuple[dict, int]:
    """Stage 2: contract_readiness_check.py --body-file <body> --mode execute."""
    result = subprocess.run(
        [
            sys.executable,
            str(CONTRACT_READINESS_CHECK_PY),
            "--body-file",
            str(body_file),
            "--mode",
            "execute",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.stdout, f"No stdout from contract_readiness_check.py (stderr: {result.stderr})"
    return json.loads(result.stdout), result.returncode


def _run_merge(review_result: dict, readiness_result: dict, tmp_path: Path) -> tuple[dict, int]:
    """Stage 3: check_issue_contract.py --mode merge_readiness."""
    review_file = tmp_path / "review.json"
    readiness_file = tmp_path / "readiness.json"
    review_file.write_text(json.dumps(review_result), encoding="utf-8")
    readiness_file.write_text(json.dumps(readiness_result), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_ISSUE_CONTRACT_PY),
            "--mode",
            "merge_readiness",
            "--review-result-file",
            str(review_file),
            "--readiness-result-file",
            str(readiness_file),
            "--readiness-artifact-path",
            str(readiness_file),
            "--iteration-id",
            "pr1878-integration-test",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.stdout, f"No stdout from merge_readiness (stderr: {result.stderr})"
    return json.loads(result.stdout), result.returncode


def _run_full_pipeline(tmp_path: Path, name: str, body: str) -> dict:
    """Run all 3 stages against `body` and return the merged final result."""
    body_file = _write_body(tmp_path, name, body)
    review_result, _review_exit = _run_review(body_file)
    readiness_result, _readiness_exit = _run_readiness(body_file)
    merged_result, _merge_exit = _run_merge(review_result, readiness_result, tmp_path)
    return {
        "review": review_result,
        "readiness": readiness_result,
        "merged": merged_result,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_parent_no_vc_approves_with_c4_and_c8_na(tmp_path: Path) -> None:
    stages = _run_full_pipeline(tmp_path, "valid_parent_no_vc.md", _TRUE_CANONICAL_PARENT_BASE)
    review = stages["review"]
    merged = stages["merged"]

    assert review["deterministic_checks"]["C4_vc_commands_present"] == "n/a", review
    assert review["deterministic_checks"]["C8_outcome_concreteness"] == "n/a", review
    assert merged["verdict"] == "approve", (
        f"Expected approve verdict for a true canonical parent body with no VC "
        f"section, got: {merged['verdict']}. blocking_issues={merged.get('blocking_issues')}"
    )


def test_valid_parent_failing_vc_does_not_approve(tmp_path: Path) -> None:
    stages = _run_full_pipeline(
        tmp_path, "valid_parent_failing_vc.md", _TRUE_CANONICAL_PARENT_FAILING_VC
    )
    readiness = stages["readiness"]
    merged = stages["merged"]

    assert readiness["status"] != "go", (
        f"Expected non-'go' readiness status for a canonical parent body with a "
        f"failing VC, got: {readiness['status']}"
    )
    assert merged["verdict"] != "approve", (
        f"Expected non-approve verdict for a canonical parent body with a failing "
        f"`## Verification Commands` section, got: {merged['verdict']}"
    )


def test_implementation_missing_outcome_needs_fix(tmp_path: Path) -> None:
    stages = _run_full_pipeline(
        tmp_path, "implementation_missing_outcome.md", _IMPLEMENTATION_MISSING_OUTCOME
    )
    merged = stages["merged"]
    assert merged["verdict"] == "needs-fix", (
        f"Expected needs-fix verdict for an implementation body missing "
        f"`## Outcome`, got: {merged['verdict']}. "
        f"blocking_issues={merged.get('blocking_issues')}"
    )


def test_implementation_missing_vc_needs_fix(tmp_path: Path) -> None:
    stages = _run_full_pipeline(
        tmp_path, "implementation_missing_vc.md", _IMPLEMENTATION_MISSING_VC
    )
    merged = stages["merged"]
    assert merged["verdict"] == "needs-fix", (
        f"Expected needs-fix verdict for an implementation body missing "
        f"`## Verification Commands`, got: {merged['verdict']}. "
        f"blocking_issues={merged.get('blocking_issues')}"
    )


def test_malformed_mrc_duplicate_key_does_not_approve(tmp_path: Path) -> None:
    stages = _run_full_pipeline(tmp_path, "malformed.md", _MALFORMED_MRC_DUPLICATE_KEY)
    review = stages["review"]
    merged = stages["merged"]
    assert review["issue_kind"] == "unknown_issue_kind", review
    assert merged["verdict"] != "approve", (
        f"Expected non-approve verdict for malformed (duplicate-key) MRC, "
        f"got: {merged['verdict']}"
    )


def test_unknown_issue_kind_does_not_approve(tmp_path: Path) -> None:
    stages = _run_full_pipeline(tmp_path, "unknown.md", _UNKNOWN_ISSUE_KIND)
    review = stages["review"]
    merged = stages["merged"]
    assert review["issue_kind"] == "unknown_issue_kind", review
    assert merged["verdict"] != "approve", (
        f"Expected non-approve verdict for an unknown issue_kind body, "
        f"got: {merged['verdict']}"
    )


def test_decoy_yaml_in_notes_does_not_approve(tmp_path: Path) -> None:
    """A decoy `issue_kind: parent` YAML block under an unrelated `## Notes`
    section must not cause this genuinely-incomplete implementation body
    (missing Verification Commands / Stop Conditions / Allowed Paths /
    Runtime Verification Applicability) to be treated as a complete parent
    and approved."""
    stages = _run_full_pipeline(tmp_path, "decoy.md", _DECOY_YAML_IN_NOTES)
    review = stages["review"]
    merged = stages["merged"]
    assert review["issue_kind"] == "implementation", (
        "Decoy YAML in '## Notes' must not override the real MRC section's "
        f"issue_kind: implementation, got: {review['issue_kind']}"
    )
    assert merged["verdict"] != "approve", (
        f"Expected non-approve verdict for decoy-YAML body, got: {merged['verdict']}"
    )


def test_body_sha256_consistent_across_all_three_stages(tmp_path: Path) -> None:
    stages = _run_full_pipeline(tmp_path, "valid_parent_no_vc.md", _TRUE_CANONICAL_PARENT_BASE)
    review_sha = stages["review"]["body_sha256"]
    readiness_sha = stages["readiness"]["body_sha256"]
    merged_sha = stages["merged"]["body_sha256"]

    assert review_sha, "review body_sha256 must not be empty"
    assert review_sha == readiness_sha == merged_sha, (
        f"body_sha256 must be identical across review ({review_sha!r}), "
        f"readiness ({readiness_sha!r}) and merged ({merged_sha!r}) stages"
    )
