#!/usr/bin/env python3
"""
Test fail_closed detection for malformed contracts.

AC10: Malformed / missing_required_section / unknown_input_schema fixtures
should produce fail_closed.required=true with appropriate reason_codes.
"""

import json
import subprocess
from pathlib import Path
from typing import Any


def fixture_to_input(
    fixture_name: str, fixture_type: str, issue_number: int
) -> dict[str, Any]:
    """Convert markdown fixture to input JSON."""
    fixture_path = (
        Path(__file__).parent.parent
        / "fixtures"
        / fixture_type
        / f"{fixture_name}.md"
    )

    body = fixture_path.read_text(encoding="utf-8") if fixture_path.exists() else ""

    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": f"Test: {fixture_name}",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
    }


def run_planner(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Run planner and return output with exit code."""
    script_path = (
        Path(__file__).parent.parent
        / "scripts"
        / "plan_refinement_loop.py"
    )

    input_json = json.dumps(input_data, ensure_ascii=False)
    result = subprocess.run(
        ["python3", str(script_path)],
        input=input_json,
        capture_output=True,
        text=True,
    )

    return json.loads(result.stdout), result.returncode


class TestFailClosed:
    """Test fail_closed detection."""

    def test_broken_machine_readable_contract(self):
        """AC10: Detect malformed machine-readable contract."""
        input_data = fixture_to_input("broken_machine_readable_contract", "malformed", 1)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0, "Should exit 0 even with fail_closed.required=true"
        assert output["fail_closed"]["required"] is True
        assert (
            "malformed_machine_readable_contract"
            in output["fail_closed"]["reason_codes"]
        )
        assert len(output["fail_closed"]["human_message"]) > 0

    def test_missing_outcome_section(self):
        """AC10: Detect missing required Outcome section."""
        input_data = fixture_to_input("missing_outcome_section", "malformed", 2)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        assert "missing_required_section" in output["fail_closed"]["reason_codes"]

    def test_invalid_input_schema(self):
        """AC10: Detect unknown input schema."""
        invalid_input = {
            "schema_version": "wrong_version",
            "issue": {"number": 1},
        }

        script_path = (
            Path(__file__).parent.parent
            / "scripts"
            / "plan_refinement_loop.py"
        )

        input_json = json.dumps(invalid_input, ensure_ascii=False)
        result = subprocess.run(
            ["python3", str(script_path)],
            input=input_json,
            capture_output=True,
            text=True,
        )

        output = json.loads(result.stdout)
        assert result.returncode == 2
        assert output["fail_closed"]["required"] is True
        assert "unknown_input_schema" in output["fail_closed"]["reason_codes"]

    def test_fail_closed_still_returns_json(self):
        """AC10: Even with fail_closed.required=true, output is valid JSON."""
        input_data = fixture_to_input("missing_outcome_section", "malformed", 3)
        output, _ = run_planner(input_data)

        # Should have all required fields
        assert "schema_version" in output
        assert "source" in output
        assert "decisions" in output
        assert "fail_closed" in output

    def test_good_fixture_does_not_fail_closed(self):
        """AC10: Good fixtures still return the canonical planner shape."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 4)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["schema_version"] == "refinement_loop_plan/v1"
        assert "fail_closed" in output
        assert "decisions" in output


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
