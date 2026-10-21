#!/usr/bin/env python3
"""
Test that planner execution is idempotent.

AC9: Same input produces identical JSON output (excluding timestamp fields).
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


def run_planner(input_data: dict[str, Any]) -> dict[str, Any]:
    """Run planner and return output."""
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

    return json.loads(result.stdout)


class TestIdempotent:
    """Test idempotency of planner execution."""

    @staticmethod
    def normalize_for_comparison(output: dict[str, Any]) -> str:
        """Normalize output for comparison (remove timestamp)."""
        # Remove generated_at since it changes per run
        output_copy = json.loads(json.dumps(output))
        output_copy["source"]["generated_at"] = "NORMALIZED"
        return json.dumps(output_copy, sort_keys=True, ensure_ascii=False)

    def test_idempotent_repo_path_in_outcome(self):
        """AC9: Same input always produces same output."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)

        output1 = run_planner(input_data)
        output2 = run_planner(input_data)
        output3 = run_planner(input_data)

        normalized1 = self.normalize_for_comparison(output1)
        normalized2 = self.normalize_for_comparison(output2)
        normalized3 = self.normalize_for_comparison(output3)

        assert (
            normalized1 == normalized2 == normalized3
        ), "Outputs should be identical"

    def test_idempotent_critical_external_claim(self):
        """AC9: Same input always produces same output."""
        input_data = fixture_to_input("critical_external_claim_in_vc", "positive", 2)

        output1 = run_planner(input_data)
        output2 = run_planner(input_data)

        normalized1 = self.normalize_for_comparison(output1)
        normalized2 = self.normalize_for_comparison(output2)

        assert normalized1 == normalized2, "Outputs should be identical"

    def test_idempotent_delivery_rollup(self):
        """AC9: Same input always produces same output."""
        input_data = fixture_to_input("delivery_rollup_unmaterialized", "positive", 3)

        output1 = run_planner(input_data)
        output2 = run_planner(input_data)

        normalized1 = self.normalize_for_comparison(output1)
        normalized2 = self.normalize_for_comparison(output2)

        assert normalized1 == normalized2, "Outputs should be identical"

    def test_issue_body_sha256_consistent(self):
        """AC9: issue_body_sha256 should be consistent across runs."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)

        output1 = run_planner(input_data)
        output2 = run_planner(input_data)

        assert (
            output1["source"]["issue_body_sha256"]
            == output2["source"]["issue_body_sha256"]
        ), "issue_body_sha256 should be identical"

    def test_deterministic_target_paths_order(self):
        """AC9: target_paths should be sorted consistently."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)

        output1 = run_planner(input_data)
        output2 = run_planner(input_data)

        paths1 = output1["decisions"]["investigation_policy"]["target_paths"]
        paths2 = output2["decisions"]["investigation_policy"]["target_paths"]

        assert paths1 == paths2, "target_paths should be in same order"
        assert paths1 == sorted(paths1), "target_paths should be sorted"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
