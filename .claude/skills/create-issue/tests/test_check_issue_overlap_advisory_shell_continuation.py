"""#1869 fix_delta P0-3: canonical create-issue shell invocation must
continue under `set -euo pipefail` regardless of `check_issue_overlap.py`
decision (safe_new_issue / overlap_requires_comment / ambiguous_requires_human
/ duplicate).

This is a real subprocess behavior test (bash -c with set -euo pipefail),
not a string-grep assertion: it proves that removing `--fail-on-unsafe`
from the SKILL.md canonical invocation actually prevents shell abort for
every non-safe decision, using --dry-run + --candidates-file (no network).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "check_issue_overlap.py"


def _run_under_pipefail(tmp_path: Path, candidates: list[dict], title: str, goal: str = "goal") -> tuple[int, dict]:
    candidates_file = tmp_path / "candidates.json"
    candidates_file.write_text(json.dumps(candidates), encoding="utf-8")

    # This mirrors the canonical create-issue SKILL.md invocation shape
    # (without --fail-on-unsafe) inside a `set -euo pipefail` shell, so a
    # non-zero exit anywhere in the pipeline would abort the script.
    script = (
        f"set -euo pipefail\n"
        f'OUT=$(uv run --locked python3 "{HELPER}" '
        f'--title "{title}" --goal "{goal}" '
        f'--candidates-file "{candidates_file}" --dry-run)\n'
        f"echo \"$OUT\"\n"
        f"echo AFTER_OVERLAP_CHECK_REACHED\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    return proc.returncode, proc


def test_safe_new_issue_decision_continues_under_pipefail(tmp_path):
    returncode, proc = _run_under_pipefail(tmp_path, candidates=[], title="実装: 完全に新規のタイトル")
    assert returncode == 0, proc.stderr
    assert "AFTER_OVERLAP_CHECK_REACHED" in proc.stdout


def test_ambiguous_or_duplicate_decision_still_continues_under_pipefail(tmp_path):
    """A candidate with the exact same title forces a non-safe decision
    (duplicate/ambiguous); the canonical (no --fail-on-unsafe) invocation
    must still reach the line after the overlap check.
    """
    title = "実装: 重複するタイトルのテスト"
    candidates = [
        {
            "number": 999,
            "title": title,
            "allowed_paths": [],
            "state": "OPEN",
        }
    ]
    returncode, proc = _run_under_pipefail(tmp_path, candidates=candidates, title=title)
    assert returncode == 0, proc.stderr
    assert "AFTER_OVERLAP_CHECK_REACHED" in proc.stdout
    assert (
        "duplicate" in proc.stdout
        or "ambiguous_requires_human" in proc.stdout
        or "overlap_requires_comment" in proc.stdout
    ), proc.stdout


def test_fail_on_unsafe_flag_still_available_for_opt_in_callers(tmp_path):
    """The --fail-on-unsafe flag itself must remain functional (not removed
    from the script) for any other caller that explicitly opts into hard
    gating; only the create-issue SKILL.md canonical invocation stops using
    it.
    """
    title = "実装: 重複するタイトルのテスト2"
    candidates_file = tmp_path / "candidates.json"
    candidates_file.write_text(
        json.dumps([{"number": 1000, "title": title, "allowed_paths": [], "state": "OPEN"}]),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable, str(HELPER),
            "--title", title, "--goal", "goal",
            "--candidates-file", str(candidates_file),
            "--dry-run", "--fail-on-unsafe",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(proc.stdout)
    assert payload["decision"] != "safe_new_issue"
    assert proc.returncode == 3
