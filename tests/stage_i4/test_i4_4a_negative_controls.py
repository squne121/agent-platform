"""I-4-4A negative controls: a missing/shifted drift policy must be a loud import failure.

Each case runs in a clean `python -I` subprocess against a copied layout, so no other test's
sys.path / sys.modules state can mask the failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTER = ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
POLICY = "scripts/agent-ops/main_drift_policy.py"

_PROBE = (
    "import importlib.util,sys\n"
    "s=importlib.util.spec_from_file_location('r',sys.argv[1])\n"
    "m=importlib.util.module_from_spec(s); sys.modules['r']=m; s.loader.exec_module(m)\n"
    "print(m.route_loop_verdict_v2.__name__)\n"
)

_APPROVE = (
    "import importlib.util,sys\n"
    "s=importlib.util.spec_from_file_location('r',sys.argv[1])\n"
    "m=importlib.util.module_from_spec(s); sys.modules['r']=m; s.loader.exec_module(m)\n"
    "sha='a'*40\n"
    "d=m.route_loop_verdict_v2({'verdict':'APPROVE','reviewed_head_sha':sha,'blockers':[]},"
    "{'head_sha':sha,'mergeable':'MERGEABLE','merge_state_status':'CLEAN'})\n"
    "print(d.route)\n"
)


def _layout(tmp: Path, *, with_policy: bool = True, router_prefix: str = "") -> Path:
    dest = tmp / router_prefix / ROUTER
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / ROUTER, dest)
    if with_policy:
        (tmp / POLICY).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / POLICY, tmp / POLICY)
    return dest


def _run(code: str, router: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(router)], capture_output=True, text=True, check=False
    )


def test_canonical_layout_positive_control(tmp_path: Path) -> None:
    r = _run(_APPROVE, _layout(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "approved"


def test_missing_main_drift_policy_is_a_loud_import_failure(tmp_path: Path) -> None:
    r = _run(_PROBE, _layout(tmp_path, with_policy=False))
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr
    assert "approved" not in r.stdout


def test_shifted_layout_cannot_find_the_policy(tmp_path: Path) -> None:
    """The router moved one level deeper no longer resolves parents[4]/scripts/agent-ops."""
    r = _run(_PROBE, _layout(tmp_path, router_prefix="nested"))
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr
