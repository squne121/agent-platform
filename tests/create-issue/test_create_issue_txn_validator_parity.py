"""AC3: create_issue_txn.py forwards --kind / --title to its internal validator so
kind-specific required sections / Stop Conditions / title prefix are fail-closed even when
the caller forgot to pre-validate.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import create_issue_txn as txn
import pytest

SCRIPTS_DIR = Path(txn.__file__).resolve().parent
VALIDATOR = SCRIPTS_DIR / "validate_issue_body.py"


class _CapturedRun:
    def __init__(self):
        self.argv = None

    def __call__(self, argv, *a, **kw):
        self.argv = argv

        class R:
            returncode = 0
            stdout = '{"status": "pass", "errors": []}'
            stderr = ""

        return R()


def test_validator_invocation_forwards_kind_and_title(monkeypatch):
    cap = _CapturedRun()
    monkeypatch.setattr(txn.subprocess, "run", cap)
    txn._run_issue_body_validator("body text", issue_kind="implementation", title="実装: x")
    assert "--kind" in cap.argv
    assert cap.argv[cap.argv.index("--kind") + 1] == "implementation"
    assert "--title" in cap.argv
    assert cap.argv[cap.argv.index("--title") + 1] == "実装: x"


def test_validator_invocation_omits_kind_when_absent(monkeypatch):
    cap = _CapturedRun()
    monkeypatch.setattr(txn.subprocess, "run", cap)
    txn._run_issue_body_validator("body text")
    assert "--kind" not in cap.argv
    assert "--title" not in cap.argv


def test_adopts_mrc_kind_when_issue_kind_omitted(monkeypatch):
    # High 1 (#946): when --issue-kind is omitted, the body MRC issue_kind is adopted.
    cap = _CapturedRun()
    monkeypatch.setattr(txn.subprocess, "run", cap)
    body = "```yaml\nissue_kind: implementation\n```\nbody"
    txn._run_issue_body_validator(body, issue_kind="")
    assert "--kind" in cap.argv
    assert cap.argv[cap.argv.index("--kind") + 1] == "implementation"


def test_issue_kind_mrc_mismatch_fails_closed(monkeypatch):
    # High 1 (#946): an explicit --issue-kind that contradicts the MRC kind is fail-closed.
    cap = _CapturedRun()
    monkeypatch.setattr(txn.subprocess, "run", cap)
    body = "```yaml\nissue_kind: research\n```\nbody"
    with pytest.raises(txn.TransactionError, match="issue_kind mismatch"):
        txn._run_issue_body_validator(body, issue_kind="implementation")
    # No validator subprocess is spawned when the mismatch is detected.
    assert cap.argv is None


# Behavioral proof that parity matters: a body that omits a kind-specific required section
# passes the validator WITHOUT --kind but FAILS with --kind=implementation.
_BODY_MISSING_KIND_SECTION = """## Acceptance Criteria

- [ ] AC1: x

## Verification Commands

```bash
# AC1
echo hi
```

## Allowed Paths

- `src/x.ts`
"""


def _run_validator(extra: list[str]) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(_BODY_MISSING_KIND_SECTION)
        bf = f.name
    try:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "--body-file", bf, *extra],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(bf).unlink(missing_ok=True)


def test_kind_changes_validator_outcome():
    without_kind = _run_validator([])
    with_kind = _run_validator(["--kind", "implementation", "--title", "実装: x"])
    # Without kind the minimal static section set is satisfied (pass);
    # with kind the full implementation template is enforced (fail).
    assert without_kind.returncode == 0, without_kind.stdout
    assert with_kind.returncode == 1, with_kind.stdout
