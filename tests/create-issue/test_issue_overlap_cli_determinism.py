"""AC8: 「決定論的 helper」契約 — CLI query 生成の決定性と --fail-on-unsafe gating。"""

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
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "create-issue" / "allowed-paths.txt"


def test_title_tokens_is_sorted_tuple():
    toks = cio._title_tokens("実装: Overlap Preflight standard ABC")
    assert isinstance(toks, tuple)
    assert list(toks) == sorted(toks)


def test_title_tokens_deterministic_across_calls():
    a = cio._title_tokens("実装: zeta alpha mike bravo")
    b = cio._title_tokens("実装: bravo mike alpha zeta")
    assert a == b  # 語順非依存・決定的


def test_cli_determinism_across_subprocesses(tmp_path):
    # PYTHONHASHSEED を変えても token 順は不変
    outs = []
    for seed in ("0", "1", "12345"):
        env = {**_base_env(), "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--title",
                "実装: gamma alpha delta beta",
                "--dry-run",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        outs.append(json.loads(proc.stdout)["target"]["title"])
    assert len(set(outs)) == 1


def test_fail_on_unsafe_returns_nonzero_for_overlap(tmp_path):
    candidates = [
        {
            "number": 900,
            "title": "実装: 別件",
            "state": "OPEN",
            "allowed_paths": [".claude/skills/create-issue/scripts/check_issue_overlap.py"],
        }
    ]
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps(candidates), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--title",
            "実装: overlap fixture",
            "--allowed-paths-file",
            str(FIXTURE),
            "--candidates-file",
            str(cand),
            "--dry-run",
            "--fail-on-unsafe",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 3


def test_fail_on_unsafe_zero_for_safe(tmp_path):
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps([]), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--title",
            "実装: 完全新規",
            "--allowed-paths-file",
            str(FIXTURE),
            "--candidates-file",
            str(cand),
            "--dry-run",
            "--fail-on-unsafe",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


def _base_env():
    import os

    return dict(os.environ)
