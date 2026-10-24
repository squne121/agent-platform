"""V2 parent-produced compact regression tests (Issue #2054)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402


SHA = "sha256:" + "e" * 64


def test_given_semantic_approve_when_parent_runs_then_v2_compact_and_artifact_are_bound(tmp_path: Path):
    result = transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", "import json; print(json.dumps({'verdict':'approve','blocking_issues':[]}))"],
        command_id="issue-reviewer.run", argv_template_id="issue-reviewer.run/v2",
        backend="fixture", issue_number=2054, repo="example-org/example-repo",
        reviewed_body_sha256=SHA, artifact_root=tmp_path, invocation_id="compact-parent",
    )
    assert result["transport_status"] == "ok"
    compact = result["attempts"][0]["compact"]
    assert compact["SCHEMA"] == transport.SCHEMA_V2
    assert compact["ARTIFACT"].startswith("compact_review_result_v2=2054/compact-parent/")


def test_given_retired_v1_producer_when_invoked_then_no_downgrade_fallback_exists():
    from compact_review_result import main

    assert main() == 2
