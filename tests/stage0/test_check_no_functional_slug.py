"""Stage 0: the slug/home-path checker fails on violations (exit 1), passes clean input (0)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "check_no_functional_slug.py"
LEGACY = "squne121/" + "loop-protocol"  # allow-legacy-slug
REFS = "refs/" + "loop-protocol/heads"  # allow-legacy-slug


def _run(*args):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_violating_python_string_exits_1(tmp_path):
    (tmp_path / "bad.py").write_text(f'SLUG = "{LEGACY}"\n')
    assert _run(tmp_path).returncode == 1


def test_violating_legacy_ref_in_json_exits_1(tmp_path):
    (tmp_path / "bad.json").write_text(f'{{"p": "{REFS}"}}\n')
    assert _run(tmp_path).returncode == 1


def test_comment_and_markdown_are_exempt_and_clean_exits_0(tmp_path):
    (tmp_path / "ok.py").write_text(f"# mentions {LEGACY}\nX = 1\n")
    (tmp_path / "note.md").write_text(f"{LEGACY}\n")
    assert _run(tmp_path).returncode == 0


def test_missing_root_exits_2(tmp_path):
    assert _run(tmp_path / "absent").returncode == 2


def test_nothing_to_scan_exits_2(tmp_path):
    (tmp_path / "note.md").write_text("x\n")
    assert _run(tmp_path).returncode == 2


def test_repository_is_clean():
    r = _run(
        ROOT / "scripts", ROOT / "config", ROOT / "tests", ROOT / ".github", ROOT / "pyproject.toml"
    )
    assert r.returncode == 0, r.stdout


def test_concatenated_and_uppercase_legacy_slug_are_detected(tmp_path):
    (tmp_path / "cat.py").write_text('X = "squne121/" + "loop-protocol"\n')
    assert _run(tmp_path).returncode == 1
    (tmp_path / "cat.py").unlink()
    (tmp_path / "up.json").write_text(f'{{"s": "{LEGACY.upper()}"}}\n')
    assert _run(tmp_path).returncode == 1


def test_worktrees_named_production_dir_is_scanned(tmp_path):
    d = tmp_path / "scripts" / "worktrees"
    d.mkdir(parents=True)
    (d / "a.py").write_text('X = "squne121/" + "loop-protocol"\n')
    assert _run(tmp_path).returncode == 1


def test_marker_does_not_hide_violation_outside_allowed_paths(tmp_path):
    (tmp_path / "prod.py").write_text('X = "squne121/" + "loop-protocol"  # allow-legacy-slug\n')
    assert _run(tmp_path).returncode == 1


def test_uppercase_extension_is_scanned(tmp_path):
    (tmp_path / "D.PY").write_text('X = "squne121/" + "loop-protocol"\n')
    assert _run(tmp_path).returncode == 1


def test_undecodable_target_is_an_error_not_clean(tmp_path):
    (tmp_path / "ok.py").write_text("X = 1\n")
    (tmp_path / "broken.json").write_bytes(b'{"s": "\xff\xfe"}')
    r = _run(tmp_path)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "broken.json" in r.stderr


def test_unreadable_python_target_is_an_error_not_clean(tmp_path):
    (tmp_path / "broken.py").write_bytes(b"X = '\xff'\n")
    assert _run(tmp_path).returncode == 2
