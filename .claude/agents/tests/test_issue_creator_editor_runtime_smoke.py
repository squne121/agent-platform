"""Issue #2046 AC7: real-runtime lane for the `issue-creator` persona,
mirroring `test_issue_editor_runtime_smoke.py`'s `issue-editor` coverage --
runtime smoke evidence that `issue-creator` is actually launched as the
active Claude Code session persona (`claude --agent issue-creator -p ...`),
and that its canonical Skill body (create-issue/SKILL.md) is actually read
in that persona-bound session.

This is a pytest wrapper around `.claude/skills/worktree-agent-runtime-smoke`
(`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`). It does not
fabricate runtime evidence: a real `claude --agent issue-creator -p`
structured-lane subprocess is launched against a linked worktree via the
runner's opt-in `--claude-agent-name` flag, and the test's outcome is
derived strictly from the runner's exit code and its persisted `summary.md`
evidence file.

Issue #2046 AC8: the target worktree is resolved from an
explicitly-declared "current candidate worktree" -- the
`RUNTIME_SMOKE_CANDIDATE_WORKTREE` environment variable -- never a fixed
reference to one historical Issue's worktree slug, and never an
auto-discovered arbitrary worktree (see
`test_issue_editor_runtime_smoke.py`'s module docstring for the rationale).
SKIP (never FAIL) when the env var is unset or invalid.

Runtime Verification Applicability (live Issue #2046 body):
decision=immediate, applicable_acs=[AC7]. Per
`docs/dev/runtime-verification-policy.md`, an unavailable runtime/capability
is SKIP (exit 77), never promoted to PASS. This dedicated new test file
follows Issue #1285/#1960's VC-contract convention of not appending
real-runtime coverage to a deterministic/fake-runtime suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

_CANDIDATE_WORKTREE_ENV_VAR = "RUNTIME_SMOKE_CANDIDATE_WORKTREE"

EXPECT_MARKER = "RUNTIME_SMOKE_ISSUE_CREATOR_READ_OK"

PROMPT = f"""You are running as a bounded, non-interactive runtime smoke check.

Use the Read tool to read the file `.claude/skills/create-issue/SKILL.md`
(repo-relative to your current working directory) in full.

After the read succeeds, output the single literal line:
{EXPECT_MARKER}

Do not output anything else after that line. Do not attempt to edit, write,
or mutate any file.
"""


def _resolve_candidate_worktree() -> Path | None:
    """Issue #2046 AC8: an explicitly-declared current candidate worktree
    -- never an auto-discovered, arbitrary worktree. Returns ``None`` (never
    a guess) when the env var is unset or the path is not a real worktree
    checkout."""
    raw = os.environ.get(_CANDIDATE_WORKTREE_ENV_VAR)
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_dir() and (candidate / ".git").exists():
        return candidate
    return None


def test_canonical_skill_read_smoke():
    """AC7: real read of create-issue/SKILL.md, observed via a fresh Claude
    Code structured-lane runtime smoke subprocess bound to the
    `issue-creator` persona.
    """
    worktree = _resolve_candidate_worktree()
    if worktree is None:
        pytest.skip(
            f"real-runtime lane requires {_CANDIDATE_WORKTREE_ENV_VAR} to point at "
            "an explicit linked worktree; unset or invalid here"
        )

    with tempfile.TemporaryDirectory(prefix="runtime-smoke-issue-creator-") as tmp:
        tmp_path = Path(tmp)
        prompt_file = tmp_path / "issue-creator-smoke-prompt.md"
        prompt_file.write_text(PROMPT, encoding="utf-8")

        output_dir = tmp_path / "output" / f"issue-creator-smoke-{int(time.time())}"

        argv = [
            sys.executable,
            str(RUNNER),
            "--runtime",
            "claude",
            "--mode",
            "structured",
            "--worktree",
            str(worktree),
            "--prompt-file",
            str(prompt_file),
            "--output-dir",
            str(output_dir),
            "--timeout-seconds",
            "180",
            "--max-turns",
            "8",
            "--agent-type",
            "issue-creator",
            "--claude-agent-name",
            "issue-creator",
            "--expect-marker",
            EXPECT_MARKER,
            "--require-clean-postcondition",
        ]

        result = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )

        if result.returncode == 77:
            pytest.skip(
                "worktree-agent-runtime-smoke SKIP (exit 77): capability/auth/"
                f"herdr unavailable. stderr={result.stderr.strip()[-2000:]}"
            )

        assert result.returncode == 0, (
            "worktree-agent-runtime-smoke did not report success "
            f"(exit={result.returncode}).\nstdout={result.stdout[-2000:]}\n"
            f"stderr={result.stderr[-2000:]}"
        )

        summary_path = output_dir / "summary.md"
        assert summary_path.is_file(), f"expected persisted evidence file at {summary_path}"
        summary_text = summary_path.read_text(encoding="utf-8")
        assert summary_text.strip(), "summary.md must not be empty"
        # Issue #2046 AC1/AC4/AC7: never promoted from SKIP; only asserted
        # on a genuine exit 0 (real runtime capability confirmed available).
        assert "main_agent_identity" in summary_text
        assert "skill_evidence" in summary_text
