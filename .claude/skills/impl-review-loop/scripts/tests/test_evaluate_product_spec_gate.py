#!/usr/bin/env python3
"""
Unit tests for evaluate_product_spec_gate.py

Fixture-driven test suite covering all required AC7 fixture cases:
- pass (applicability=applicable, decision=pass)
- not_applicable (applicability=not_applicable)
- fail (decision=fail)
- human_judgment (decision=human_judgment)
- missing-schema (product/spec trigger present but product_spec_check absent)
- stale-snapshot (schema mismatch)
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


def load_fixture(fixture_name: str) -> Dict[str, Any]:
    """Load fixture JSON from fixtures/ directory."""
    fixture_dir = Path(__file__).parent / "fixtures"
    fixture_path = fixture_dir / f"{fixture_name}.json"
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def run_gate_evaluator(fixture_data: Dict[str, Any], contract_snapshot_url: str = None) -> Dict[str, Any]:
    """Run evaluate_product_spec_gate.py with fixture data via stdin."""
    script_path = Path(__file__).parent.parent / "evaluate_product_spec_gate.py"

    cmd = [sys.executable, str(script_path), "--snapshot-json", "-"]
    if contract_snapshot_url:
        cmd.extend(["--contract-snapshot-url", contract_snapshot_url])

    result = subprocess.run(
        cmd,
        input=json.dumps(fixture_data),
        capture_output=True,
        text=True,
        timeout=10,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Script failed: {result.stderr}")

    output = result.stdout.strip()
    return json.loads(output)


def test_pass_continues():
    """AC7: PASS fixture — applicability=applicable, decision=pass → routing_action: continue"""
    fixture = load_fixture("pass")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "continue", f"Expected routing_action=continue, got {result['routing_action']}"
    assert result["decision"] == "pass", f"Expected decision=pass, got {result['decision']}"
    assert result["applicability"] == "applicable", f"Expected applicability=applicable, got {result['applicability']}"


def test_not_applicable_continues():
    """AC7: NOT_APPLICABLE fixture — applicability=not_applicable → routing_action: continue"""
    fixture = load_fixture("not_applicable")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "continue", f"Expected routing_action=continue, got {result['routing_action']}"
    assert result["applicability"] == "not_applicable", (
        f"Expected applicability=not_applicable, got {result['applicability']}"
    )


def test_fail_stops_human():
    """AC7: FAIL fixture — decision=fail → routing_action: stop_human with blocked_rule_ids normalized from
        blocked_reasons
    """
    fixture = load_fixture("fail")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "stop_human", (
        f"Expected routing_action=stop_human, got {result['routing_action']}"
    )
    assert result["decision"] == "fail", f"Expected decision=fail, got {result['decision']}"
    assert len(result["blocked_rule_ids"]) > 0, "Expected blocked_rule_ids to be non-empty"
    # Verify blocked_rule_ids were normalized from blocked_reasons
    assert "PS001" in result["blocked_rule_ids"], (
        f"Expected PS001 in blocked_rule_ids, got {result['blocked_rule_ids']}"
    )
    assert "PS002" in result["blocked_rule_ids"], (
        f"Expected PS002 in blocked_rule_ids, got {result['blocked_rule_ids']}"
    )


def test_human_judgment_stops_human():
    """AC7: HUMAN_JUDGMENT fixture — decision=human_judgment → routing_action: stop_human with blocked_rule_ids
        normalized from blocked_reasons
    """
    fixture = load_fixture("human_judgment")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "stop_human", (
        f"Expected routing_action=stop_human, got {result['routing_action']}"
    )
    assert result["decision"] == "human_judgment", f"Expected decision=human_judgment, got {result['decision']}"
    # Verify blocked_rule_ids were normalized from blocked_reasons
    assert len(result["blocked_rule_ids"]) > 0, "Expected blocked_rule_ids to be non-empty"
    assert "PS006" in result["blocked_rule_ids"], (
        f"Expected PS006 in blocked_rule_ids, got {result['blocked_rule_ids']}"
    )


def test_missing_schema_refreshes():
    """AC7: MISSING-SCHEMA fixture — product_spec_check absent → routing_action: refresh_contract_snapshot"""
    fixture = load_fixture("missing-schema")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "refresh_contract_snapshot", (
        f"Expected routing_action=refresh_contract_snapshot, got {result['routing_action']}"
    )
    assert result["decision"] == "missing", f"Expected decision=missing, got {result['decision']}"
    assert result["applicability"] == "missing", f"Expected applicability=missing, got {result['applicability']}"


def test_missing_schema_preserves_body_sha256():
    """Blocker 3: MISSING-SCHEMA fixture must preserve body_sha256 even when product_spec_check absent"""
    fixture = load_fixture("missing-schema")
    result = run_gate_evaluator(fixture)

    expected_body_sha256 = fixture.get("body_sha256")
    assert result["body_sha256"] == expected_body_sha256, (
        f"Expected body_sha256={expected_body_sha256}, got {result['body_sha256']}"
    )


def test_stale_snapshot_refreshes():
    """AC7: STALE-SNAPSHOT fixture — invalid enum (decision: blocked) → routing_action: refresh_contract_snapshot"""
    fixture = load_fixture("stale-snapshot")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "refresh_contract_snapshot", (
        f"Expected routing_action=refresh_contract_snapshot, got {result['routing_action']}"
    )
    assert result["reason"] == "Invalid product_spec_check enum value", (
        f"Expected reason about invalid enum, got {result['reason']}"
    )


def test_missing_contract_root_refreshes():
    """Blocker 2: MISSING-CONTRACT-ROOT fixture — CONTRACT_REVIEW_RESULT_V1 root absent → routing_action:
        refresh_contract_snapshot
    """
    fixture = load_fixture("missing-contract-root")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "refresh_contract_snapshot", (
        f"Expected routing_action=refresh_contract_snapshot, got {result['routing_action']}"
    )
    assert result["decision"] == "missing", f"Expected decision=missing, got {result['decision']}"
    assert result["applicability"] == "missing", f"Expected applicability=missing, got {result['applicability']}"


def test_not_applicable_fail_refreshes():
    """Blocker 1: NOT_APPLICABLE_FAIL fixture — applicability=not_applicable, decision=fail → routing_action:
        refresh_contract_snapshot
    """
    fixture = load_fixture("not_applicable_fail")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "refresh_contract_snapshot", (
        f"Expected routing_action=refresh_contract_snapshot, got {result['routing_action']}"
    )
    assert result["applicability"] == "not_applicable", (
        f"Expected applicability=not_applicable, got {result['applicability']}"
    )
    assert result["decision"] == "fail", f"Expected decision=fail, got {result['decision']}"
    assert result["reason"] == "Inconsistent product_spec_check: not_applicable requires decision=pass", (
        f"Expected consistency check reason, got {result.get('reason')}"
    )


def test_not_applicable_human_judgment_refreshes():
    """Blocker 1: NOT_APPLICABLE_HUMAN_JUDGMENT fixture — applicability=not_applicable, decision=human_judgment →
        routing_action: refresh_contract_snapshot
    """
    fixture = load_fixture("not_applicable_human_judgment")
    result = run_gate_evaluator(fixture)

    assert result["routing_action"] == "refresh_contract_snapshot", (
        f"Expected routing_action=refresh_contract_snapshot, got {result['routing_action']}"
    )
    assert result["applicability"] == "not_applicable", (
        f"Expected applicability=not_applicable, got {result['applicability']}"
    )
    assert result["decision"] == "human_judgment", f"Expected decision=human_judgment, got {result['decision']}"
    assert result["reason"] == "Inconsistent product_spec_check: not_applicable requires decision=pass", (
        f"Expected consistency check reason, got {result.get('reason')}"
    )


def test_contract_snapshot_url_cli_passthrough():
    """Blocker 2: CLI argument --contract-snapshot-url is passed through to output"""
    fixture = load_fixture("pass")
    cli_url = "https://github.com/example-org/example-repo/issues/333#issuecomment-9999999"
    result = run_gate_evaluator(fixture, contract_snapshot_url=cli_url)

    assert result["contract_snapshot_url"] == cli_url, (
        f"Expected contract_snapshot_url={cli_url}, got {result['contract_snapshot_url']}"
    )
    # Verify issue_url is still present for back-compat
    assert "issue_url" in result, "Expected issue_url field to be present for back-compat"


# Issue #333 VC node name aliases (AC2 and AC4)
def test_decision_fail_stops_human():
    """AC2 alias: Issue #333 VC requires test_decision_fail_stops_human node name"""
    test_fail_stops_human()


def test_missing_or_stale_routes_refresh():
    """AC4 alias: Issue #333 VC requires test_missing_or_stale_routes_refresh node name"""
    test_missing_schema_refreshes()
    test_stale_snapshot_refreshes()
    test_missing_contract_root_refreshes()


if __name__ == "__main__":
    # Run all tests
    tests = [
        test_pass_continues,
        test_not_applicable_continues,
        test_fail_stops_human,
        test_human_judgment_stops_human,
        test_missing_schema_refreshes,
        test_missing_schema_preserves_body_sha256,
        test_stale_snapshot_refreshes,
        test_missing_contract_root_refreshes,
        test_not_applicable_fail_refreshes,
        test_not_applicable_human_judgment_refreshes,
        test_contract_snapshot_url_cli_passthrough,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1

    print(f"\nTests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
