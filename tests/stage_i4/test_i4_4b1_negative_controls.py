"""I-4-4B1 negative controls: broken/shifted dependency layouts must fail loudly.

Each case runs in a clean `python -I` subprocess against a copied layout, so no other test's
sys.path / sys.modules state can mask the failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"
ROUTER = ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
POLICY = "scripts/agent-ops/main_drift_policy.py"

_PROBE = (
    "import importlib.util,sys\n"
    "s=importlib.util.spec_from_file_location('a',sys.argv[1])\n"
    "m=importlib.util.module_from_spec(s); sys.modules['a']=m; s.loader.exec_module(m)\n"
    "m._load_route_module()\n"
    "print('loaded')\n"
)


def _layout(tmp: Path, *, with_router: bool = True, prefix: str = "") -> Path:
    dest = tmp / prefix / SCRIPT
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / SCRIPT, dest)
    if with_router:
        shutil.copy(ROOT / ROUTER, dest.parent / "route_loop_verdict_v2.py")
        (tmp / prefix / POLICY).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / POLICY, tmp / prefix / POLICY)
    return dest


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_canonical_layout_positive_control(tmp_path: Path) -> None:
    r = _run(_layout(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "loaded"


def test_missing_router_is_a_loud_failure(tmp_path: Path) -> None:
    r = _run(_layout(tmp_path, with_router=False))
    assert r.returncode != 0
    assert "loaded" not in r.stdout
