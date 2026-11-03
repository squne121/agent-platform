"""I-4-2 negative controls: a missing producer dependency or a shifted layout must be detectable.

Each case runs in a clean `python -I` subprocess against a copied layout, so no other test's
sys.path / sys.modules state can mask the failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ECS = ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
GATE = ".claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py"
SHARED = ".claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py"
MATCHER = "scripts/agent-guards/changed_file_matcher.py"
POLICY = "scripts/agent-ops/allowed_paths_policy.py"
ALL = (ECS, GATE, SHARED, MATCHER, POLICY)

_PROBE = (
    "import importlib.util,sys\n"
    "s=importlib.util.spec_from_file_location('ecs',sys.argv[1])\n"
    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
    "m._import_allowed_paths_gate_module()\n"
)


def _layout(tmp: Path, *, skip: str | None = None, ecs_prefix: str = "") -> Path:
    for rel in ALL:
        if rel == skip:
            continue
        dest = tmp / (ecs_prefix if rel in (ECS, SHARED) else "") / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp / ecs_prefix / ECS


def _probe(ecs: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(ecs)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_canonical_layout_positive_control(tmp_path: Path) -> None:
    r = _probe(_layout(tmp_path))
    assert r.returncode == 0, r.stderr


def test_missing_allowed_paths_gate_is_detected(tmp_path: Path) -> None:
    r = _probe(_layout(tmp_path, skip=GATE))
    assert r.returncode != 0 and "FileNotFoundError" in r.stderr


def test_missing_changed_file_matcher_is_detected(tmp_path: Path) -> None:
    r = _probe(_layout(tmp_path, skip=MATCHER))
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr


def test_shifted_layout_is_detected(tmp_path: Path) -> None:
    """The publisher moved one level away from its sibling skills no longer finds the gate."""
    for rel in ALL:
        if rel in (ECS, SHARED):
            continue
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    for rel in (ECS, SHARED):
        dest = tmp_path / "x" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    r = _probe(tmp_path / "x" / ECS)
    assert r.returncode != 0 and "FileNotFoundError" in r.stderr
