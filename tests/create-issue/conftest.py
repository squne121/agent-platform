"""Minimal import shim for the create-issue tests migrated in Stage 2-5 (#35).

The source conftest also carried child-materialization plan fixtures; those belong to
Stage 2-6 / #36 (materialize_child_issues / plan_child_materialization) and are not migrated here.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
