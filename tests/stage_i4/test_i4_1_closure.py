"""I-4-1: contract review leaf producers (result parser / declared path overlap).

Fixture evidence only: no network, no `gh`, no GitHub read or write.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-1-contract-review-leaf-manifest.json"
ICR = ".claude/skills/issue-contract-review"
SCRIPTS = [
    f"{ICR}/scripts/contract_review_result_parser.py",
    f"{ICR}/scripts/declared_path_overlap.py",
]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.parametrize("rel", SCRIPTS)
def test_isolated_import(rel: str) -> None:
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('m',{rel!r});"
        "m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m)"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    assert set(SCRIPTS) <= set(m["required_files"])
    for rel, info in m["source_parity"].items():
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert digest == info["moved_sha256"], rel
        if info["verbatim"]:
            assert info["source_sha256"] == info["moved_sha256"], rel
        else:
            assert info.get("intentional_diff"), rel


def test_shared_dependencies_resolve_in_canonical_layout() -> None:
    sys.path.insert(0, str(ROOT / ICR / "scripts"))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("dpo_i4_1", ROOT / SCRIPTS[1])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        assert mod._load_path_normalizer() is not None
        assert mod._load_allowed_paths_matcher() is not None
    finally:
        sys.path.pop(0)


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", ICR],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
