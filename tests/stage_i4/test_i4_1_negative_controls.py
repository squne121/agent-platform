"""I-4-1 negative controls: a missing dependency or a shifted layout must be detectable."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REL = ".claude/skills/issue-contract-review/scripts/declared_path_overlap.py"
DEPS = [
    ".claude/skills/create-issue/scripts/check_issue_overlap.py",
    "scripts/agent-guards/changed_file_matcher.py",
    # imported by check_issue_overlap.py via sys.path (scripts/agent-ops); not a tested dependency
    "scripts/agent-ops/allowed_paths_policy.py",
]


@pytest.fixture(autouse=True)
def _isolate_import_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore sys.path / sys.modules so no test depends on another test's imports."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    sys.modules.pop("allowed_paths_policy", None)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _layout(tmp: Path, *, skip: str | None = None, shift: str = "") -> Path:
    for rel in [REL, *DEPS]:
        if rel == skip:
            continue
        dest = tmp / rel
        if rel == REL and shift:
            dest = tmp / Path(rel).parent / shift / Path(rel).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp / REL if not shift else tmp / Path(REL).parent / shift / Path(REL).name


def test_canonical_layout_positive_control(tmp_path: Path) -> None:
    mod = _load(_layout(tmp_path), "dpo_neg_ok")
    assert mod._load_path_normalizer() is not None
    assert mod._load_allowed_paths_matcher() is not None


def test_missing_check_issue_overlap_is_detected(tmp_path: Path) -> None:
    mod = _load(_layout(tmp_path, skip=DEPS[0]), "dpo_neg_a")
    assert mod._load_path_normalizer() is None


def test_missing_changed_file_matcher_is_detected(tmp_path: Path) -> None:
    mod = _load(_layout(tmp_path, skip=DEPS[1]), "dpo_neg_b")
    assert mod._load_allowed_paths_matcher() is None


def test_layout_shift_is_detected(tmp_path: Path) -> None:
    mod = _load(_layout(tmp_path, shift="extra"), "dpo_neg_c")
    assert mod._load_path_normalizer() is None
    assert mod._load_allowed_paths_matcher() is None
