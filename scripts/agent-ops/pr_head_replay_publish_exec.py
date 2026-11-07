"""Git merge-tree conflict oracle (I-4-4C4, oracle-only migration; no ref, index or worktree writes).

Migrated from the pre-split source repository's scripts/agent-ops/pr_head_replay_publish_exec.py
(tag pre-split-agent-platform-20261020, commit 6fa07b6a0ab932a62f08db6a8d521c0dfded2075; the
manifest docs/dev/i4-4c4-merge-tree-oracle-manifest.json records the source blob and digests).
Only compute_semantic_ambiguity(), _merge_tree_conflicts() and the minimal ``_run``
helper are kept. The source's replay / publish executor (execute(), main(), GitHub
PR/Issue reads, remote fetch, worktree, commit, push / ref update) is deliberately NOT
migrated and has no entry point here.

The only Git command issued is ``git merge-tree --write-tree BASE CANDIDATE``: exit 0 =
no conflict, exit 1 = conflict, anything else = Git error. Every non-zero exit is reported
as ambiguity (fail-closed); an error is never read as "no conflict" (an invalid ref also exits
1, so a conflict and an invalid ref are indistinguishable by exit code). A runner exception
(timeout, missing cwd) propagates unchanged and is never turned into False. merge-tree may write
tree objects to the object database, so this is not a strictly filesystem-read-only call;
it does not touch the index, the worktree or any ref.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    argv: Sequence[str], *, cwd: Path, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        input=input,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _merge_tree_conflicts(runner: Runner, root: Path, base_sha: str, candidate_sha: str) -> bool:
    """Deterministic real-conflict oracle (Issue #2102 P1-C).

    Uses ``git merge-tree --write-tree`` (worktree/index-free) to decide
    whether ``candidate_sha`` conflicts against ``base_sha``. This replaces
    caller-supplied boolean ``semantic_ambiguity`` flags with an actual Git
    merge computation. A nonzero exit from the two-ref form of
    ``merge-tree --write-tree`` means the merge produced conflicts.
    """
    proc = runner(["git", "merge-tree", "--write-tree", base_sha, candidate_sha], cwd=root)
    return proc.returncode != 0


def compute_semantic_ambiguity(
    base_sha: str,
    candidate_sha: str,
    *,
    cwd: Path,
    runner: Runner = _run,
) -> bool:
    """Public export of the deterministic real-conflict oracle (Issue #2102
    fix_delta iteration 5, Blocker B).

    ``route_loop_verdict_v2.py`` (a pure, side-effect-free module by design;
    see its module docstring) has no subprocess authority of its own, so it
    cannot compute ``semantic_ambiguity`` itself. This is the exact-file
    Allowed Paths counterpart that CAN: it wraps ``_merge_tree_conflicts()``
    (unchanged) as a stable public entry point a caller-side wrapper in
    ``route_loop_verdict_v2.py`` imports, rather than duplicating the
    ``git merge-tree --write-tree`` probe.
    """
    # I-4-4C4 intentional difference (fail-closed hardening, not in the source): a ref that starts
    # with "-" would be parsed by Git as an option (``--stdin`` makes merge-tree exit 0 for any
    # input), so it is reported as ambiguity without running Git.
    if str(base_sha).startswith("-") or str(candidate_sha).startswith("-"):
        return True
    return _merge_tree_conflicts(runner, cwd, base_sha, candidate_sha)
