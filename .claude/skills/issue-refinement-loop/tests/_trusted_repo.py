"""Trusted repository slug for tests that drive the real produce path.

`run_root_review_pipeline` binds `--repo` to `scripts/agent-guards/repo_identity.py` (fail-closed),
so tests that run it end to end must use the configured slug instead of a hard-coded one.
"""

import sys
from pathlib import Path

_GUARDS = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"
if str(_GUARDS) not in sys.path:
    sys.path.insert(0, str(_GUARDS))
import repo_identity  # noqa: E402

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()
