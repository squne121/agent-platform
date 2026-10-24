#!/usr/bin/env python3
"""Retired V1 compact producer (Issue #2054).

`compact_review_result()` (the V1 `ISSUE_REVIEW_RESULT_COMPACT_V1` renderer)
and this module's CLI are retired. `run_root_review_pipeline.py`'s `produce`
subcommand now builds and persists `ISSUE_REVIEW_RESULT_COMPACT_V2` via
`reviewer_transport.py` (the V2 contract SSOT) instead. Retaining the V1
renderer would allow a V1/V2 partial deployment, so this module's CLI fails
closed instead of translating input; there is no downgrade fallback (AC5).

`_atomic_write()` is kept here: it is a generic hardened writer primitive
(mkstemp + 0600 + symlink recheck) unrelated to the V1/V2 wire format
itself. `run_root_review_pipeline.py`'s `persist_to_canonical_artifact_directory()`
(for the unrelated `ROOT_REVIEW_PIPELINE_RESULT_V1` artifact, not the
compact envelope) still reuses it, so removing it would break that
producer's own artifact persistence, which is out of this Issue's scope.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_CANONICAL_ARTIFACT_DIR = Path(".claude/artifacts/issue-refinement-loop")


def _reject_canonical_symlink_components(root: Path, issue_slot: str) -> None:
    """Reject existing symlinks in the canonical artifact hierarchy."""
    current = root
    for component in (*_CANONICAL_ARTIFACT_DIR.parts, issue_slot):
        current = current / component
        try:
            os.lstat(current)
        except FileNotFoundError:
            continue
        if os.path.islink(current):
            raise ValueError("artifact_symlink_component_rejected")


def _atomic_write(
    path: Path,
    content: bytes,
    *,
    canonical_root: Path | None = None,
    issue_slot: str | None = None,
) -> None:
    """Write content atomically with 0600 permissions."""
    if canonical_root is not None and issue_slot is not None:
        _reject_canonical_symlink_components(canonical_root, issue_slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    if canonical_root is not None and issue_slot is not None:
        _reject_canonical_symlink_components(canonical_root, issue_slot)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> int:
    print(
        "compact_review_result.py is retired: ISSUE_REVIEW_RESULT_COMPACT_V1 has no "
        "producer or downgrade fallback (Issue #2054 AC5). Use the parent-owned "
        "reviewer_transport.py (ISSUE_REVIEW_RESULT_COMPACT_V2) via "
        "run_root_review_pipeline.py produce.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
