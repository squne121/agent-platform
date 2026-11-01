"""AC6: GitHub source（search / read-back）の失敗・partial・saturation を
fail-closed（ambiguous_requires_human）に倒すことを検証する。

`safe_new_issue` は source 成功かつ overlap 0 のときだけ返す。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio

HELPER = SCRIPTS_DIR / "check_issue_overlap.py"


def _current():
    return cio.IssueScope(title="実装: 新規 helper", allowed_paths=("src/state/new.ts",))


def test_search_failed_is_fail_closed():
    ss = cio.SourceStatus(issue_search=cio.SOURCE_FAILED)
    result = cio.classify_overlap(_current(), [], ss)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.reason_code == "source_failed"
    assert result.policy_class == "unknown"


def test_readback_partial_is_fail_closed():
    ss = cio.SourceStatus(issue_readback=cio.SOURCE_PARTIAL)
    result = cio.classify_overlap(_current(), [], ss)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.reason_code == "readback_partial"


def test_search_saturated_is_fail_closed():
    ss = cio.SourceStatus(issue_search=cio.SOURCE_SATURATED)
    result = cio.classify_overlap(_current(), [], ss)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN


def test_source_ok_zero_candidates_is_safe():
    result = cio.classify_overlap(_current(), [], cio.SourceStatus.ok())
    assert result.verdict == cio.SAFE_NEW_ISSUE
    assert result.source_status.issue_search == cio.SOURCE_OK


def test_cli_candidates_file_propagates_degraded_source_status(tmp_path):
    payload = {
        "source_status": {"issue_search": "failed", "issue_readback": "ok"},
        "candidates": [],
    }
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--title",
            "実装: x",
            "--candidates-file",
            str(cand),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(proc.stdout)
    assert out["decision"] == "ambiguous_requires_human"
    assert out["source_status"]["issue_search"] == "failed"


def test_gh_adapter_failure_returns_failed_status(monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(cio.subprocess, "run", boom)
    candidates, status = cio.gh_search_candidates("o/r", ("token",))
    assert candidates == []
    assert status.issue_search == cio.SOURCE_FAILED
    # その status を渡すと fail-closed
    assert (
        cio.classify_overlap(_current(), candidates, status).verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    )
