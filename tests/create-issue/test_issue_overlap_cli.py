"""AC1: check_issue_overlap.py の CLI が title / Allowed Paths 入力から
overlap verdict を返すことを subprocess 経由で検証する。

CLI は fixture の allowed-paths ファイルを `--allowed-paths-file` で受け取り、
offline（--dry-run / --candidates-file）で決定論的に verdict を返す。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))
import repo_identity

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "check_issue_overlap.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "create-issue" / "allowed-paths.txt"

VERDICTS = {
    "duplicate",
    "overlap_requires_comment",
    "safe_new_issue",
    "ambiguous_requires_human",
}


def _run_cli(*extra: str) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--repo",
            TRUSTED_REPO,
            "--title",
            "実装: overlap fixture",
            "--allowed-paths-file",
            str(FIXTURE),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_helper_and_fixture_exist():
    assert HELPER.is_file(), f"missing helper: {HELPER}"
    assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"


def test_cli_dry_run_no_candidates_returns_safe_new_issue():
    out = _run_cli("--dry-run")
    assert out["mode"] == "issue_overlap"
    assert out["decision"] in VERDICTS
    # 候補なしの dry-run は safe_new_issue
    assert out["decision"] == "safe_new_issue"


def test_cli_dry_run_with_overlapping_candidate(tmp_path):
    candidates = [
        {
            "number": 900,
            "title": "実装: overlap preflight の前段",
            "state": "OPEN",
            "allowed_paths": [".claude/skills/create-issue/scripts/check_issue_overlap.py"],
        }
    ]
    cand_file = tmp_path / "candidates.json"
    cand_file.write_text(json.dumps(candidates), encoding="utf-8")
    out = _run_cli("--dry-run", "--candidates-file", str(cand_file))
    assert out["decision"] in VERDICTS
    assert out["decision"] == "overlap_requires_comment"
    assert 900 in out["matched_issues"]


def test_cli_exit_code_zero_for_computed_verdict():
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--title",
            "実装: overlap fixture",
            "--allowed-paths-file",
            str(FIXTURE),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
