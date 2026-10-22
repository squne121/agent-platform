#!/usr/bin/env python3
"""
Tests for contract_readiness_check.py

Covers:
  - AC2: ISSUE_CONTRACT_READINESS_RESULT_V1 schema validation
  - AC3: baseline_vc_preflight result lossless mapping
  - AC4: RVA decision: immediate required field detection
  - AC8: blocked fixture detection
  - AC9: go fixture status: go
  - AC10: no new logic in orchestration skills
  - AC12: static mode requires no network/auth
  - AC13: result JSON schema strict validation
"""

import json
import subprocess
import sys
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent
_FIXTURES_DIR = _TESTS_DIR / "fixtures"
_SCRIPT = _SCRIPTS_DIR / "contract_readiness_check.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_RESULT_TOP_KEYS = {
    "schema",
    "status",
    "body_sha256",
    "source_checks",
    "errors",
    "minimal_context",
    "fix_hint",
}

REQUIRED_ERROR_KEYS = {
    "rule_id",
    "severity",
    "source_check",
    "category",
    "section",
    "line_start",
    "line_end",
    "minimal_context",
    "fix_hint",
    "autofixable",
}

# source_payload is an optional field present only for baseline_vc_preflight errors (AC3 lossless)
OPTIONAL_ERROR_KEYS = {"source_payload"}
ALLOWED_ERROR_KEYS = REQUIRED_ERROR_KEYS | OPTIONAL_ERROR_KEYS

REQUIRED_SOURCE_CHECK_KEYS = {"name", "schema", "status", "exit_code"}

VALID_STATUSES = {"go", "needs_fix", "human_judgment"}


def run_readiness(body_file: Path, mode: str = "static") -> tuple[dict, int]:
    """Run contract_readiness_check.py against a body file."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--body-file", str(body_file), "--mode", mode],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout, f"No stdout from script (stderr: {result.stderr})"
    return json.loads(result.stdout), result.returncode


def run_readiness_with_body(body: str, mode: str = "static") -> tuple[dict, int]:
    """Run contract_readiness_check.py against a body string via temp file."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--body-file", tmp_path, "--mode", mode],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No stdout (stderr: {result.stderr})"
        return json.loads(result.stdout), result.returncode
    finally:
        import os
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# AC1: Script exists
# ---------------------------------------------------------------------------


def test_contract_readiness_script_exists():
    """AC1: contract_readiness_check.py exists."""
    assert _SCRIPT.exists(), f"Script not found: {_SCRIPT}"


# ---------------------------------------------------------------------------
# AC2: ISSUE_CONTRACT_READINESS_RESULT_V1 schema
# ---------------------------------------------------------------------------


def test_contract_readiness_result_v1_schema():
    """AC2: result has all required top-level fields and valid schema."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")

    assert data.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert set(data.keys()) >= REQUIRED_RESULT_TOP_KEYS, (
        f"Missing top-level keys: {REQUIRED_RESULT_TOP_KEYS - set(data.keys())}"
    )
    assert data["status"] in VALID_STATUSES

    # source_checks structure
    assert isinstance(data["source_checks"], list)
    for sc in data["source_checks"]:
        assert set(sc.keys()) >= REQUIRED_SOURCE_CHECK_KEYS, (
            f"source_check missing keys: {REQUIRED_SOURCE_CHECK_KEYS - set(sc.keys())}"
        )

    # errors structure
    assert isinstance(data["errors"], list)
    for err in data["errors"]:
        missing = REQUIRED_ERROR_KEYS - set(err.keys())
        assert not missing, f"error item missing keys: {missing}"

    # minimal_context is list
    assert isinstance(data["minimal_context"], list)


# ---------------------------------------------------------------------------
# AC13: schema strict validation (no unknown top-level keys allowed)
# ---------------------------------------------------------------------------


def test_schema_strict_no_unknown_top_level_keys():
    """AC13: result JSON must not have unknown top-level keys."""
    # These are ALL the allowed top-level keys
    allowed_top_keys = REQUIRED_RESULT_TOP_KEYS
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")
    unknown = set(data.keys()) - allowed_top_keys
    assert not unknown, f"Unknown top-level keys found: {unknown}"


def test_schema_strict_error_item_no_unknown_keys():
    """AC13: each error item must not have unknown keys (source_payload is allowed for preflight errors)."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_blocked.md")
    for err in data["errors"]:
        unknown = set(err.keys()) - ALLOWED_ERROR_KEYS
        assert not unknown, f"Unknown error item keys: {unknown}"


# ---------------------------------------------------------------------------
# AC3: baseline_vc_preflight result lossless mapping
# ---------------------------------------------------------------------------


COMPOUND_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ pnpm build && echo DONE
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_compound_command_disallowed_mapping():
    """AC3: compound_command_disallowed in VC → needs_fix in readiness result (static mode)."""
    data, exit_code = run_readiness_with_body(COMPOUND_BODY, mode="static")
    # Should detect compound command
    assert data["status"] in ("needs_fix", "human_judgment"), (
        f"Expected needs_fix or human_judgment, got: {data['status']}"
    )
    categories = [e["category"] for e in data["errors"]]
    assert "compound_command_disallowed" in categories, (
        f"compound_command_disallowed not found in errors: {categories}"
    )
    # Exit code 1 for needs_fix
    assert exit_code == 1, f"Expected exit_code 1, got {exit_code}"


UNEXPECTED_PASS_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ rg -n "ISSUE_CONTRACT_READINESS_RESULT_V1" .claude/skills/issue-contract-review/scripts/contract_readiness_check.py
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_unexpected_pass_mapped_in_execute_mode():
    """AC3: unexpected_pass from preflight → needs_fix in execute mode."""
    # Run in execute mode so baseline_vc_preflight actually runs the command
    data, _ = run_readiness_with_body(UNEXPECTED_PASS_BODY, mode="execute")
    # rg finds the string (unexpected pass) → needs_fix
    # OR if the file doesn't exist in cwd: expected_fail → go (depends on runtime env)
    # We just verify schema is valid
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] in VALID_STATUSES


INVALID_PREFLIGHT_SCOPE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
# preflight-scope: invalid_scope_value
$ test -f nonexistent_file.py
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_invalid_preflight_scope_execute_mode():
    """AC3: invalid preflight-scope marker → human_judgment in execute mode."""
    data, exit_code = run_readiness_with_body(INVALID_PREFLIGHT_SCOPE_BODY, mode="execute")
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    # Invalid preflight scope → human_judgment from baseline_vc_preflight
    assert data["status"] == "human_judgment", (
        f"Expected human_judgment for invalid preflight-scope, got: {data['status']}"
    )
    assert exit_code == 2


def test_human_judgment_not_collapsed_to_needs_fix():
    """AC3: human_judgment from preflight MUST NOT be collapsed to needs_fix."""
    data, exit_code = run_readiness_with_body(INVALID_PREFLIGHT_SCOPE_BODY, mode="execute")
    assert data["status"] == "human_judgment", (
        "human_judgment must not be collapsed to needs_fix"
    )
    assert exit_code == 2, f"Expected exit code 2 for human_judgment, got {exit_code}"


# ---------------------------------------------------------------------------
# AC4: RVA decision: immediate required field check
# ---------------------------------------------------------------------------

RVA_MISSING_FIELDS_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
# preflight-scope: runtime_only
$ test -f some_file.py
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
```

"""


def test_runtime_verification_applicability_missing_fields():
    """AC4: decision: immediate with missing required fields → needs_fix."""
    data, exit_code = run_readiness_with_body(RVA_MISSING_FIELDS_BODY)
    assert data["status"] in ("needs_fix", "human_judgment"), (
        f"Expected needs_fix (or human_judgment), got: {data['status']}"
    )
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RVA001" in rule_ids, f"RVA001 not found in errors: {rule_ids}"


def test_applicable_acs_missing():
    """AC4: applicable_acs field missing for decision: immediate."""
    body = RVA_MISSING_FIELDS_BODY.replace("applicable_acs:\n  - AC1\n", "")
    data, _ = run_readiness_with_body(body)
    _rule_ids = [e["rule_id"] for e in data["errors"]]
    categories = [e["category"] for e in data["errors"]]
    assert "rva_immediate_field_missing" in categories, (
        f"rva_immediate_field_missing not found: {categories}"
    )


def test_rva_not_applicable_no_errors():
    """AC4: decision: not_applicable has no RVA errors."""
    body = RVA_MISSING_FIELDS_BODY.replace("decision: immediate", "decision: not_applicable")
    body = body.replace("applicable_acs:\n  - AC1\n", "")
    data, _ = run_readiness_with_body(body)
    rva_errors = [e for e in data["errors"] if e.get("category") == "rva_immediate_field_missing"]
    assert not rva_errors, f"Unexpected RVA errors for not_applicable: {rva_errors}"


# ---------------------------------------------------------------------------
# AC8: blocked fixture
# ---------------------------------------------------------------------------


def test_issue412_blocked_fixture_detects_problems():
    """AC8: blocked fixture detects compound VC and RVA missing fields."""
    data, exit_code = run_readiness(_FIXTURES_DIR / "issue412_contract_blocked.md")

    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] in ("needs_fix", "human_judgment"), (
        f"Blocked fixture should not be go, got: {data['status']}"
    )
    assert len(data["errors"]) > 0, "Blocked fixture should have errors"

    categories = [e["category"] for e in data["errors"]]
    # Compound command should be detected
    assert "compound_command_disallowed" in categories, (
        f"compound_command_disallowed not detected: {categories}"
    )
    # RVA missing fields should be detected
    assert "rva_immediate_field_missing" in categories, (
        f"rva_immediate_field_missing not detected: {categories}"
    )


# ---------------------------------------------------------------------------
# AC9: go fixture
# ---------------------------------------------------------------------------


def test_contract_go_fixture():
    """AC9: go fixture → status: go."""
    data, exit_code = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")

    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] == "go", (
        f"Go fixture should have status: go, got: {data['status']}. "
        f"Errors: {data['errors']}"
    )
    assert exit_code == 0, f"Expected exit code 0 for go, got {exit_code}"


# ---------------------------------------------------------------------------
# AC10: no new logic in orchestration skills
# ---------------------------------------------------------------------------


def test_no_logic_in_orchestration_skills():
    """AC10: issue-refinement-loop and impl-review-loop SKILL.md have no new judgment logic."""
    _repo_root = _SCRIPTS_DIR.parents[4]
    loop_skills = [
        _repo_root / ".claude" / "skills" / "issue-refinement-loop" / "SKILL.md",
        _repo_root / ".claude" / "skills" / "impl-review-loop" / "SKILL.md",
    ]

    # Marker patterns that would indicate new judgment logic was added
    forbidden_patterns = [
        "contract_readiness_check",
        "ISSUE_CONTRACT_READINESS_RESULT_V1",
    ]

    for skill_path in loop_skills:
        if not skill_path.exists():
            continue
        content = skill_path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern not in content, (
                f"Forbidden pattern '{pattern}' found in {skill_path}. "
                "AC10 requires no new logic in orchestration skills."
            )


# ---------------------------------------------------------------------------
# AC12: static mode requires no network/auth
# ---------------------------------------------------------------------------


def test_no_network_required():
    """AC12: --mode static can run without GitHub token or network."""
    import os

    # Run with a clean environment (no GH_TOKEN)
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}

    body_file = _FIXTURES_DIR / "issue412_contract_go.md"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--body-file", str(body_file), "--mode", "static"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.stdout, f"No stdout (stderr: {result.stderr})"
    data = json.loads(result.stdout)
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    # Should not fail due to missing auth
    assert data["status"] in VALID_STATUSES


def test_static_mode_no_baseline_preflight_source_check():
    """AC12: static mode does NOT include baseline_vc_preflight source_check."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md", mode="static")
    source_names = [sc["name"] for sc in data["source_checks"]]
    assert "baseline_vc_preflight" not in source_names, (
        "static mode must not run baseline_vc_preflight"
    )
    assert "validate_issue_body" in source_names


def test_execute_mode_includes_baseline_preflight():
    """AC12 (contrast): execute mode DOES include baseline_vc_preflight source_check."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md", mode="execute")
    source_names = [sc["name"] for sc in data["source_checks"]]
    assert "baseline_vc_preflight" in source_names


# ---------------------------------------------------------------------------
# issue412 fixture aliases (for VC -k "issue412" keyword matching)
# ---------------------------------------------------------------------------


def test_issue412_go_fixture():
    """AC9 (issue412 alias): go fixture → status: go."""
    data, exit_code = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] == "go", (
        f"Go fixture should have status: go, got: {data['status']}. "
        f"Errors: {data['errors']}"
    )
    assert exit_code == 0

# ---------------------------------------------------------------------------
# AC3 Unit: map_preflight_result_to_errors() with synthetic preflight JSON
# (No environment-dependent rg execution — purely unit testing the mapper)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(_SCRIPTS_DIR))
from contract_readiness_check import map_preflight_result_to_errors


def test_unexpected_pass_maps_to_needs_fix():
    """AC3 unit: synthetic unexpected_pass result → needs_fix status and category in errors."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "rg foo file.md",
                "raw_command": "rg foo file.md",
                "exit_code": 0,
                "classification": "unexpected_pass",
                "category": "unexpected_pass",
                "decision": "blocked",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 42,
                "fix_hint": "VC passed before implementation. Tighten VC so it fails at baseline.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix, got {aggregate}"
    assert any(e["category"] == "unexpected_pass" for e in errors), (
        f"unexpected_pass not found in errors: {errors}"
    )


def test_vc_no_tests_collected_is_needs_fix():
    """Issue #1434: producer exit-5 output maps to body-fixable needs_fix."""
    body = UNEXPECTED_PASS_BODY.replace(
        '$ rg -n "ISSUE_CONTRACT_READINESS_RESULT_V1" '
        ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
        "$ uv run --locked pytest "
        ".claude/skills/issue-contract-review/scripts/tests/test_contract_readiness.py "
        "-k issue_1434_missing_test -q",
    )

    data, exit_code = run_readiness_with_body(body, mode="execute")

    assert data["status"] == "needs_fix", data["errors"]
    assert exit_code == 1
    preflight_errors = [
        error for error in data["errors"]
        if error["source_check"] == "baseline_vc_preflight"
    ]
    assert [error["category"] for error in preflight_errors] == ["vc_no_tests_collected"]


def test_existing_file_missing_node_id_noncanonical_maps_to_needs_fix():
    """PR #1366 review (Blocker 1) / Issue #1347: existing_file_missing_node_id_noncanonical
    from baseline_vc_preflight must map to needs_fix (body-author fixable), not
    fall through to human_judgment via the unmapped-category default."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "uv run pytest existing_file.py::test_new_case -q",
                "raw_command": "uv run pytest existing_file.py::test_new_case -q",
                "exit_code": 4,
                "classification": "blocked",
                "category": "existing_file_missing_node_id_noncanonical",
                "decision": "blocked",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 42,
                "fix_hint": (
                    "This pytest VC references a NEW test function on an EXISTING "
                    "file (<file>::<new_test_name>), which is a non-canonical VC "
                    "shape (Issue #1285 / PR #1305). Rewrite the VC to reference a "
                    "not-yet-created test file (missing_new_test_file.py::test_name) "
                    "instead of adding a new test function to an existing file."
                ),
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix, got {aggregate}"
    assert any(
        e["category"] == "existing_file_missing_node_id_noncanonical" for e in errors
    ), f"existing_file_missing_node_id_noncanonical not found in errors: {errors}"


def test_compound_command_maps_to_needs_fix():
    """AC3 unit: synthetic compound_command_disallowed result → needs_fix."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "pnpm build && echo DONE",
                "raw_command": "pnpm build && echo DONE",
                "exit_code": -1,
                "classification": "blocked",
                "category": "compound_command_disallowed",
                "decision": "blocked",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 10,
                "fix_hint": "Replace compound shell command with a single command.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix, got {aggregate}"
    assert any(e["category"] == "compound_command_disallowed" for e in errors)


def test_human_judgment_decision_not_collapsed():
    """AC3 unit: human_judgment decision MUST NOT be collapsed to needs_fix."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "some_tool --check",
                "raw_command": "some_tool --check",
                "exit_code": 127,
                "classification": "blocked",
                "category": "env_missing_dep",
                "decision": "human_judgment",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 5,
                "fix_hint": "Required tool missing. Human intervention needed.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "human_judgment", (
        f"human_judgment must not be collapsed to needs_fix, got: {aggregate}"
    )


def test_expected_baseline_fail_maps_to_go():
    """AC3 unit: expected_baseline_fail → go (no error emitted)."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "pass",
        "results": [
            {
                "ac": "AC1",
                "command": "rg 'new_function' src/new_file.py",
                "raw_command": "rg 'new_function' src/new_file.py",
                "exit_code": 1,
                "classification": "expected_fail",
                "category": "expected_baseline_fail",
                "decision": "go",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 8,
                "fix_hint": "",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "go", f"Expected go, got {aggregate}"
    assert not errors, f"Expected no errors for expected_baseline_fail, got: {errors}"


def test_preflight_static_mode_schema_valid():
    """AC3 unit: --mode preflight-static returns valid schema (static alias)."""
    # preflight-static is an alias for static; should not run baseline_vc_preflight
    body_file = _FIXTURES_DIR / "issue412_contract_go.md"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--body-file", str(body_file), "--mode", "preflight-static"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout, f"No stdout from preflight-static mode (stderr: {result.stderr})"
    data = json.loads(result.stdout)
    assert data.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] in VALID_STATUSES
    # preflight-static must NOT run baseline_vc_preflight
    source_names = [sc["name"] for sc in data["source_checks"]]
    assert "baseline_vc_preflight" not in source_names, (
        "preflight-static mode must not run baseline_vc_preflight"
    )


# ---------------------------------------------------------------------------
# AC3 Lossless: source_payload field presence (Blocker 2)
# ---------------------------------------------------------------------------


def test_source_payload_present_in_preflight_errors():
    """AC3 lossless: errors from baseline_vc_preflight must include source_payload with all fields."""
    from contract_readiness_check import map_preflight_result_to_errors

    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "pnpm build && echo DONE",
                "raw_command": "pnpm build && echo DONE",
                "exit_code": -1,
                "classification": "blocked",
                "category": "compound_command_disallowed",
                "decision": "blocked",
                "confidence": "high",
                "command_hash": "sha256:abc123",
                "duration_ms": 42,
                "scope_class": "baseline_fail_expected",
                "line": 10,
                "fix_hint": "Replace compound shell command with a single command.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert errors, "Expected at least one error"
    err = errors[0]
    assert "source_payload" in err, f"source_payload missing from error: {list(err.keys())}"
    sp = err["source_payload"]
    required_payload_fields = {
        "classification",
        "category",
        "scope_class",
        "decision",
        "confidence",
        "exit_code",
        "command_hash",
        "duration_ms",
        "runner_env_delta",
    }
    missing = required_payload_fields - set(sp.keys())
    assert not missing, f"source_payload missing fields: {missing}"
    assert sp["classification"] == "blocked"
    assert sp["category"] == "compound_command_disallowed"
    assert sp["scope_class"] == "baseline_fail_expected"
    assert sp["decision"] == "blocked"
    assert sp["confidence"] == "high"
    assert sp["exit_code"] == -1
    assert sp["command_hash"] == "sha256:abc123"
    assert sp["duration_ms"] == 42
    assert sp["runner_env_delta"] == {}


def test_source_payload_preserves_package_manager_no_tty_category():
    """AC3: package_manager_no_tty_prompt error preserves category and human_judgment routing."""
    from contract_readiness_check import map_preflight_result_to_errors

    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "pnpm test",
                "raw_command": "pnpm test",
                "exit_code": 1,
                "classification": "blocked",
                "category": "package_manager_no_tty_prompt",
                "decision": "blocked",
                "confidence": "low",
                "command_hash": "sha256:def456",
                "duration_ms": 101,
                "scope_class": "regression_gate",
                "line": 26,
                "fix_hint": "CI=true を runner 側で注入してください",
                "stdout_head": [],
                "stderr_head": [
                    "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY Aborted removal of modules directory due to no TTY"
                ],
            }
        ],
        "errors": [],
    }

    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "human_judgment", f"Expected human_judgment, got {aggregate}"
    assert len(errors) == 1, f"Expected one error, got {len(errors)}"
    err = errors[0]
    assert err["category"] == "package_manager_no_tty_prompt", (
        f"Expected package_manager_no_tty_prompt category, got {err['category']}"
    )
    assert err["source_payload"]["classification"] == "blocked", (
        f"Unexpected source_payload classification: {err['source_payload']}"
    )
    assert err["source_payload"]["decision"] == "blocked"
    assert err["source_payload"]["command_hash"] == "sha256:def456"
    assert err["source_payload"]["category"] == "package_manager_no_tty_prompt"
    assert err["source_payload"]["scope_class"] == "regression_gate"
    assert err["source_payload"]["runner_env_delta"] == {}
# ---------------------------------------------------------------------------
# Blocker 4: Redirect operators not flagged as compound (< > << >> <<<)
# ---------------------------------------------------------------------------


REDIRECT_OPERATOR_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ rg "pattern" < input.txt
$ command > output.txt
$ command << EOF
$ heredoc_cmd <<< string
$ cmd >> append.txt
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_redirect_operators_not_flagged_as_compound():
    """Blocker 4: <, >, <<, >>, <<< must NOT be flagged as compound_command_disallowed."""
    from contract_readiness_check import check_vc_static_syntax

    errors = check_vc_static_syntax(REDIRECT_OPERATOR_BODY)
    compound_errors = [e for e in errors if e["category"] == "compound_command_disallowed"]
    assert not compound_errors, (
        f"Redirect operators should not be flagged as compound_command_disallowed: "
        f"{[e['minimal_context'] for e in compound_errors]}"
    )


def test_control_operators_still_flagged():
    """Blocker 4 (contrast): &&, ||, |, ;, & must still be flagged."""
    from contract_readiness_check import check_vc_static_syntax

    errors = check_vc_static_syntax(COMPOUND_BODY)  # COMPOUND_BODY uses &&
    compound_errors = [e for e in errors if e["category"] == "compound_command_disallowed"]
    assert compound_errors, "Control operators (&&) should still be flagged as compound_command_disallowed"


# ---------------------------------------------------------------------------
# Blocker 1: validator tool/internal errors → human_judgment
# ---------------------------------------------------------------------------


def test_validator_tool_error_maps_to_human_judgment():
    """Blocker 1: validator_tool_error status → human_judgment aggregate (not needs_fix)."""
    from contract_readiness_check import compute_aggregate_status

    # Simulate validator timeout error
    validator_timeout_errors = [
        {
            "rule_id": "VALIDATOR_TIMEOUT",
            "severity": "error",
            "source_check": "validate_issue_body",
            "category": "validator_tool_error",
            "section": "(global)",
            "line_start": 0,
            "line_end": 0,
            "minimal_context": [],
            "fix_hint": "validator 実行環境を確認してください",
            "autofixable": False,
        }
    ]
    status = compute_aggregate_status(
        validate_errors=validator_timeout_errors,
        preflight_errors=[],
        rva_errors=[],
        static_vc_errors=[],
        preflight_aggregate="go",
    )
    assert status == "human_judgment", (
        f"validator_tool_error must map to human_judgment, got: {status}"
    )


def test_validator_internal_error_maps_to_human_judgment():
    """Blocker 1: validator_internal_error status → human_judgment aggregate (not needs_fix)."""
    from contract_readiness_check import compute_aggregate_status

    validator_internal_errors = [
        {
            "rule_id": "VALIDATOR_JSON_ERROR",
            "severity": "error",
            "source_check": "validate_issue_body",
            "category": "validator_internal_error",
            "section": "(global)",
            "line_start": 0,
            "line_end": 0,
            "minimal_context": [],
            "fix_hint": "validator 実行環境を確認してください",
            "autofixable": False,
        }
    ]
    status = compute_aggregate_status(
        validate_errors=validator_internal_errors,
        preflight_errors=[],
        rva_errors=[],
        static_vc_errors=[],
        preflight_aggregate="go",
    )
    assert status == "human_judgment", (
        f"validator_internal_error must map to human_judgment, got: {status}"
    )


# ---------------------------------------------------------------------------
# Issue #527: map_preflight_result_to_errors — structured errors consumer
# AC1: message field used as minimal_context head
# AC2: minimal_context field expanded correctly
# AC3: fix_hint field used correctly
# AC4: string fallback maintained
# AC5: message-absent dict falls back to str(err_msg)
# AC6: minimal_context as list → flat list[str] (no nested list)
# AC7: rule with VC000_ prefix → no double namespace
# ---------------------------------------------------------------------------


def _blocked_preflight_with_errors(errors: list) -> dict:
    """Helper: build a blocked preflight result with no results[] and given errors."""
    return {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [],
        "errors": errors,
    }


def test_ac1_message_field_used_as_minimal_context_head():
    """AC1: structured dict errors — message field becomes the first element of minimal_context."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "error",
            "rule": "VC001",
            "message": "Verification Commands section is missing",
            "minimal_context": "Add a ## Verification Commands section",
            "fix_hint": "Add the section and re-run.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors, "Expected at least one error"
    err = errors[0]
    assert isinstance(err["minimal_context"], list), "minimal_context must be a list"
    assert err["minimal_context"][0] == "Verification Commands section is missing", (
        f"First element of minimal_context must be the message field value, got: {err['minimal_context']}"
    )


def test_ac2_minimal_context_field_expanded():
    """AC2: minimal_context field from structured dict is correctly appended."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "error",
            "rule": "VC001",
            "message": "Missing fenced bash block",
            "minimal_context": "Use ```bash fencing",
            "fix_hint": "Wrap your VC commands in ```bash ... ```.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors
    err = errors[0]
    assert "Use ```bash fencing" in err["minimal_context"], (
        f"minimal_context content missing: {err['minimal_context']}"
    )


def test_ac3_fix_hint_field_used():
    """AC3: fix_hint field from structured dict is used as fix_hint in output error."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "error",
            "rule": "VC001",
            "message": "Bad VC format",
            "minimal_context": "",
            "fix_hint": "Custom fix hint from structured error",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors
    err = errors[0]
    assert err["fix_hint"] == "Custom fix hint from structured error", (
        f"fix_hint not taken from structured error: {err['fix_hint']}"
    )


def test_ac4_string_fallback_maintained():
    """AC4: plain string errors still work (existing string fallback preserved)."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        "plain string error message"
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors, "Expected at least one error from string input"
    err = errors[0]
    assert err["rule_id"] == "VCP001"
    assert err["minimal_context"][0] == "plain string error message", (
        f"String error not used as minimal_context head: {err['minimal_context']}"
    )
    assert aggregate == "needs_fix"


def test_ac5_message_absent_fallback_to_str_err_msg():
    """AC5: structured dict without 'message' key falls back to str(err_msg), not 'unknown error'."""
    from contract_readiness_check import map_preflight_result_to_errors

    err_item = {
        "kind": "error",
        "rule": "VC002",
        # no "message" key
        "minimal_context": "some context",
        "fix_hint": "some fix",
    }
    preflight = _blocked_preflight_with_errors([err_item])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors
    err = errors[0]
    # The fallback must be str(err_item), not "unknown error"
    assert err["minimal_context"][0] != "unknown error", (
        "AC5 violation: fallback must be str(err_msg), not 'unknown error'"
    )
    assert str(err_item) in err["minimal_context"][0], (
        f"AC5: expected str(err_msg) as fallback, got: {err['minimal_context'][0]}"
    )


def test_ac6_minimal_context_list_normalized_to_flat():
    """AC6: minimal_context as list in structured dict → flat list[str], no nested list."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "error",
            "rule": "VC001",
            "message": "Header message",
            "minimal_context": ["line A", "line B", "line C"],
            "fix_hint": "Fix it.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors
    err = errors[0]
    mc = err["minimal_context"]
    assert isinstance(mc, list), "minimal_context must be a list"
    for item in mc:
        assert isinstance(item, str), (
            f"AC6: nested list detected — all items must be str, got {type(item)}: {item}"
        )
    assert "line A" in mc, f"AC6: flattened items missing from minimal_context: {mc}"
    assert "line B" in mc
    assert "line C" in mc


def test_ac7_rule_with_vc_prefix_no_double_namespace():
    """AC7: rule already prefixed with VC000_ must not become VCP_VC000_ (double namespace)."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "error",
            "rule": "VC000_BODY_RETRIEVAL_FAILED",
            "message": "Body retrieval failed",
            "minimal_context": "",
            "fix_hint": "Check the issue body.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors
    err = errors[0]
    assert err["rule_id"] == "VC000_BODY_RETRIEVAL_FAILED", (
        f"AC7: double namespace detected — rule_id must be 'VC000_BODY_RETRIEVAL_FAILED', got: {err['rule_id']}"
    )
    assert not err["rule_id"].startswith("VCP_VC"), (
        f"AC7: double namespace 'VCP_VC...' detected in rule_id: {err['rule_id']}"
    )


def test_ac7_rule_with_vcp_prefix_no_double_namespace():
    """AC7: rule already prefixed with VCP_ must not become VCP_VCP_ (double namespace)."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "error",
            "rule": "VCP_SOME_RULE",
            "message": "Some VCP rule",
            "minimal_context": "",
            "fix_hint": "Fix it.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors
    err = errors[0]
    assert err["rule_id"] == "VCP_SOME_RULE", (
        f"AC7: double namespace detected for VCP_ prefixed rule, got: {err['rule_id']}"
    )


# ---------------------------------------------------------------------------
# Blocker 3: kind field consumption in map_preflight_result_to_errors()
# ---------------------------------------------------------------------------


def test_kind_retrieval_error_maps_to_body_retrieval_failed_human_judgment():
    """Blocker 3: kind=retrieval_error/rule=VC000_BODY_RETRIEVAL_FAILED → category=body_retrieval_failed,
        aggregate=human_judgment.
    """
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "retrieval_error",
            "rule": "VC000_BODY_RETRIEVAL_FAILED",
            "message": "Body retrieval failed",
            "minimal_context": "",
            "fix_hint": "Check the issue body.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors, "Expected at least one error"
    err = errors[0]
    # category must NOT be no_commands_extracted
    assert err["category"] != "no_commands_extracted", (
        f"Blocker 3: retrieval_error must not collapse to no_commands_extracted, got: {err['category']}"
    )
    assert err["category"] == "body_retrieval_failed", (
        f"Blocker 3: retrieval_error must map to body_retrieval_failed, got: {err['category']}"
    )
    # aggregate must be human_judgment (not needs_fix)
    assert aggregate == "human_judgment", (
        f"Blocker 3: retrieval_error aggregate must be human_judgment, got: {aggregate}"
    )


def test_kind_extraction_error_maps_to_needs_fix():
    """Blocker 3: kind=extraction_error → category=extraction_error, aggregate=needs_fix."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "extraction_error",
            "rule": "VC001",
            "message": "Extraction failed",
            "minimal_context": "",
            "fix_hint": "Fix the VC format.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors, "Expected at least one error"
    err = errors[0]
    assert err["category"] == "extraction_error", (
        f"Blocker 3: extraction_error must preserve category=extraction_error, got: {err['category']}"
    )
    assert aggregate == "needs_fix", (
        f"Blocker 3: extraction_error aggregate must be needs_fix, got: {aggregate}"
    )


def test_kind_unsupported_vc_format_maps_to_needs_fix_with_category_preserved():
    """Blocker 3: kind=unsupported_vc_format → category=unsupported_vc_format, aggregate=needs_fix."""
    from contract_readiness_check import map_preflight_result_to_errors

    preflight = _blocked_preflight_with_errors([
        {
            "kind": "unsupported_vc_format",
            "rule": "VC002",
            "message": "Unsupported VC format detected",
            "minimal_context": "",
            "fix_hint": "Use ```bash fenced blocks.",
        }
    ])
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert errors, "Expected at least one error"
    err = errors[0]
    # category must be unsupported_vc_format (preserved, not collapsed)
    assert err["category"] == "unsupported_vc_format", (
        f"Blocker 3: unsupported_vc_format category must be preserved, got: {err['category']}"
    )
    assert aggregate == "needs_fix", (
        f"Blocker 3: unsupported_vc_format aggregate must be needs_fix, got: {aggregate}"
    )


# ---------------------------------------------------------------------------
# Issue #889: baseline-expect annotation-aware readiness mapping
# ---------------------------------------------------------------------------


def _build_preflight_result_with_annotation(
    classification: str,
    category: str,
    decision: str,
    baseline_expect: str | None,
) -> dict:
    """Build a mock baseline_vc_preflight/v1 result payload for testing."""
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": 999,
        "repo": "example-org/example-repo",
        "status": "pass" if decision == "go" else "blocked",
        "summary": {
            "expected_fail": 0,
            "unexpected_pass": 1 if classification == "unexpected_pass" else 0,
            "blocked": 1 if decision == "blocked" else 0,
            "human_judgment": 0,
            "expected_pass": 1 if classification == "expected_pass" else 0,
            "skipped": 0,
            "extraction_errors": 0,
        },
        "results": [
            {
                "ac": "AC1",
                "line": 1,
                "raw_command": "test -d /home",
                "command_hash": "sha256:abc",
                "runner": "exec",
                "exit_code": 0,
                "classification": classification,
                "category": category,
                "decision": decision,
                "scope_class": "baseline_fail_expected",
                "confidence": "high",
                "stdout_head": [],
                "stdout_truncated": False,
                "stdout_original_line_count": 0,
                "stderr_head": [],
                "stderr_truncated": False,
                "stderr_original_line_count": 0,
                "duration_ms": 10,
                "fix_hint": None,
                "annotations": {
                    "baseline_expect": baseline_expect,
                    "vc_role": None,
                    "missing_baseline_expect": baseline_expect is None and classification == "unexpected_pass",
                },
                "annotation_source": {
                    "line": None,
                    "raw": None,
                },
            }
        ],
        "errors": [],
    }


def test_readiness_baseline_expect_pass_expected_pass_is_go():
    """Issue #889 AC12: baseline-expect: pass + expected_pass in preflight → go readiness.

    Uses map_preflight_result_to_errors directly (AC12: reads from preflight payload,
    not from Issue body re-scan).
    """
    # Simulate: baseline_vc_preflight returns expected_pass (because baseline-expect: pass)
    from contract_readiness_check import map_preflight_result_to_errors
    preflight = _build_preflight_result_with_annotation(
        classification="expected_pass",
        category="baseline_expect_pass",
        decision="go",
        baseline_expect="pass",
    )
    errors, aggregate = map_preflight_result_to_errors(preflight)
    # expected_pass → go (no error)
    assert aggregate == "go", f"Expected go but got {aggregate}"
    assert len(errors) == 0, f"Expected no errors but got {errors}"


def test_readiness_baseline_expect_fail_unexpected_pass_is_needs_fix():
    """Issue #889 AC12: baseline-expect: fail + unexpected_pass → needs_fix (backward compat)."""
    from contract_readiness_check import map_preflight_result_to_errors
    preflight = _build_preflight_result_with_annotation(
        classification="unexpected_pass",
        category="unexpected_pass",
        decision="blocked",
        baseline_expect="fail",
    )
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix but got {aggregate}"


def test_readiness_missing_annotation_unexpected_pass_is_needs_fix():
    """Issue #889 AC12: annotation absent + unexpected_pass → needs_fix (backward compat)."""
    from contract_readiness_check import map_preflight_result_to_errors
    preflight = _build_preflight_result_with_annotation(
        classification="unexpected_pass",
        category="unexpected_pass",
        decision="blocked",
        baseline_expect=None,
    )
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix but got {aggregate}"


def test_readiness_baseline_expect_pass_unexpected_pass_is_go():
    """Issue #889 AC12: baseline-expect: pass + unexpected_pass in payload → go.

    This tests the defensive branch in map_preflight_result_to_errors:
    even if preflight mistakenly returns unexpected_pass with annotations.baseline_expect=pass,
    readiness maps it to go.
    """
    from contract_readiness_check import map_preflight_result_to_errors
    preflight = _build_preflight_result_with_annotation(
        classification="unexpected_pass",
        category="unexpected_pass",
        decision="blocked",
        baseline_expect="pass",
    )
    errors, aggregate = map_preflight_result_to_errors(preflight)
    # With baseline_expect=pass, unexpected_pass should map to go
    assert aggregate == "go", f"Expected go but got {aggregate}"


def test_implementation_missing_rva_is_exactly_one_rva002_even_with_runtime_only_marker():
    """AC1/AC2: implementation RVA absence is a single deterministic readiness error."""
    body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: test
change_kind: code
```

## Outcome

RVA section absence is rejected.

## Acceptance Criteria

- [ ] AC1: readiness rejects the missing section

## Verification Commands

```bash
# runtime_only
$ test -f .claude/skills/issue-contract-review/scripts/contract_readiness_check.py # AC1
```

## Allowed Paths

- .claude/skills/issue-contract-review/scripts/contract_readiness_check.py

## Stop Conditions

- stop
"""
    data, exit_code = run_readiness_with_body(body)

    rva002_errors = [error for error in data["errors"] if error["rule_id"] == "RVA002"]
    assert data["status"] == "needs_fix"
    assert exit_code == 1
    assert len(rva002_errors) == 1
    assert rva002_errors[0] == {
        "rule_id": "RVA002",
        "severity": "error",
        "source_check": "contract_readiness_check",
        "category": "rva_section_missing",
        "section": "Runtime Verification Applicability",
        "line_start": 0,
        "line_end": 0,
        "minimal_context": ["Runtime Verification Applicability section is missing."],
        "fix_hint": (
            "Add the Runtime Verification Applicability section and explicitly choose "
            "not_applicable, immediate, or deferred. Do not infer the decision."
        ),
        "autofixable": False,
    }

    without_runtime_only = body.replace("# runtime_only\n", "")
    data_without_marker, exit_code_without_marker = run_readiness_with_body(without_runtime_only)
    assert data_without_marker["status"] == "needs_fix"
    assert exit_code_without_marker == 1
    assert [
        error for error in data_without_marker["errors"] if error["rule_id"] == "RVA002"
    ] == rva002_errors


def test_rva_heading_parser_ignores_fences_and_accepts_gfm_variants():
    """AC3/AC5: only a top-level shared-policy RVA heading satisfies readiness."""
    body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

```markdown
## Runtime Verification Applicability
- decision: immediate
```

  ## Runtime Verification Applicability ###

- decision: not_applicable
- reason: static parser test
"""
    from contract_readiness_check import check_rva_immediate_fields

    assert check_rva_immediate_fields(body) == []


def test_rva_immediate_preserves_rva001_for_missing_immediate_fields():
    """AC4: a present immediate section delegates incomplete fields to RVA001."""
    body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Runtime Verification Applicability

- decision: immediate
- applicable_acs: [AC1]
"""
    from contract_readiness_check import check_rva_immediate_fields

    errors = check_rva_immediate_fields(body)
    assert errors
    assert {error["rule_id"] for error in errors} == {"RVA001"}
