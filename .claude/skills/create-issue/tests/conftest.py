"""Fixtures for the create-issue source tests migrated in Stage 2-5b (#35).

These source tests drive the transaction engine through hand-written `run_command` / `_run_gh_json` /
`_issue_create` doubles that predate the created-issue body readback and know nothing about the
read-only GET it issues. They target parent / label / dependency / dedupe behaviour, so the readback
is neutralised here ONLY; it is exercised against the real subprocess fake `gh` (stored_body /
get_override fault injection) in tests/stage2/test_stage2_5_create_txn.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _neutralise_created_issue_body_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    import create_issue_txn as txn

    monkeypatch.setattr(txn, "_readback_created_issue", lambda *_a, **_k: None)
