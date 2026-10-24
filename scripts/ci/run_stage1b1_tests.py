"""Run the Stage 1b-1 / 1b-2 (manifest-selected) pytest lane with false-green guards.

1. Every manifest `required_files` / `required_fixtures` entry must exist (a missing policy YAML,
   matcher or evaluator is a closure defect, not a degrade).
2. `pytest --co` over the explicit `test_files` must exit 0 and the per-file counts must equal
   `expected_collect` exactly (no dir recursion: later stages add files to the same dirs).
3. Every `deferred_tests` id must be collected and carry reason / owner stage / re-run condition;
   they are the ONLY deselected tests.
4. The real run must exit 0 with JUnit tests == sum(`expected_ran`) and 0 skipped / xfailed /
   failed / errored.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "stage1b1-leaf-manifest.json"
# Stage 1b-2 reuses this lane: `run_stage1b1_tests.py docs/dev/stage1b2-contract-checkers-manifest.json`.
if len(sys.argv) > 1:
    MANIFEST = (ROOT / sys.argv[1]).resolve()
    if ROOT not in MANIFEST.parents or not MANIFEST.is_file():
        sys.exit(f"FAIL: manifest must be an existing file inside the repo: {sys.argv[1]}")


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-W",
            "ignore::SyntaxWarning",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = m["test_files"]
    deferred = m["deferred_tests"]
    for e in deferred:
        if not all(e.get(k) for k in ("id", "owner_stage", "reason", "rerun_condition")):
            print(f"FAIL: incomplete deferred entry {e.get('id')!r}")
            return 1
    missing = [
        f
        for f in [*m["required_files"], *m["required_fixtures"], *files]
        if not (ROOT / f).is_file()
    ]
    if missing:
        print(f"FAIL: closure files missing: {missing}")
        return 1
    if set(files) != set(m["expected_collect"]) or set(files) != set(m["expected_ran"]):
        print("FAIL: manifest test_files / expected_collect / expected_ran keys differ")
        return 1

    co = _pytest("--co", "-q", *files)
    if co.returncode != 0:
        print(co.stdout + co.stderr)
        print(f"FAIL: collect exit code {co.returncode} (5 = collected nothing)")
        return 1
    ids = [ln for ln in co.stdout.splitlines() if "::" in ln]
    actual = dict(Counter(i.split("::")[0] for i in ids))
    if actual != m["expected_collect"]:
        print(f"FAIL: collect mismatch\n expected={m['expected_collect']}\n actual={actual}")
        return 1
    stale = [e["id"] for e in deferred if e["id"] not in set(ids)]
    if stale:
        print(f"FAIL: deferred ids not collected: {stale}")
        return 1
    deferred_ids = {e["id"] for e in deferred}
    expected_ran = sum(m["expected_ran"].values())
    if len(ids) - len(deferred_ids) != expected_ran or expected_ran < 1:
        print("FAIL: expected_ran inconsistent with collected - deferred")
        return 1

    deselect = [f"--deselect={i}" for i in sorted(deferred_ids)]
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        run = _pytest("-q", "-rsxX", f"--junitxml={junit}", *deselect, *files)
        print(run.stdout + run.stderr)
        if run.returncode != 0:
            print(f"FAIL: pytest exit code {run.returncode}")
            return 1
        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        skipped = sum(int(s.get("skipped", "0")) for s in suites)
        bad = sum(int(s.get("errors", "0")) + int(s.get("failures", "0")) for s in suites)
        ran = sum(int(s.get("tests", "0")) for s in suites)
    if skipped != 0 or bad != 0 or ran != expected_ran:
        print(f"FAIL: skipped={skipped} bad={bad} ran={ran} expected_ran={expected_ran}")
        return 1
    print(
        f"{MANIFEST.name}: OK ({len(ids)} collected, {len(deferred_ids)} deferred with reasons, "
        f"{ran} ran, 0 skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
