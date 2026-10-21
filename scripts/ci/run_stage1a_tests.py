"""Run the Stage 1a-core pytest lane with false-green guards.

1. `pytest --co` must exit 0 and the per-file collected counts must equal
   docs/dev/stage1a-core-manifest.json `expected_collect_total` exactly.
2. Every `deferred_tests` id must exist in the collection (stale entry = FAIL) and carry a
   reason, owner stage and re-run condition. They are the ONLY deselected tests.
3. The real run must exit 0 with JUnit tests == collected - deferred == sum(`expected_ran`), and 0 skipped / xfailed
   (xfail is reported as skipped by JUnit) / failed / errored.
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
MANIFEST = ROOT / "docs" / "dev" / "stage1a-core-manifest.json"


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
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    dirs = manifest["test_dirs"]
    deferred = manifest["deferred_tests"]
    for entry in deferred:
        if not all(entry.get(k) for k in ("id", "owner_stage", "reason", "rerun_condition")):
            print(f"FAIL: incomplete deferred entry {entry.get('id')!r}")
            return 1

    present = {f.name for d in dirs for f in (ROOT / d).rglob("test_*.py")}
    listed_but_present = sorted(present & set(manifest["not_migrated_source_tests"]["files"]))
    if listed_but_present:
        print(f"FAIL: not_migrated_source_tests lists migrated files: {listed_but_present}")
        return 1

    co = _pytest("--co", "-q", *dirs)
    if co.returncode != 0:
        print(co.stdout + co.stderr)
        print(f"FAIL: collect exit code {co.returncode} (5 = collected nothing)")
        return 1
    ids = [ln for ln in co.stdout.splitlines() if "::" in ln]
    actual = Counter(i.split("::")[0] for i in ids)
    if dict(actual) != manifest["expected_collect_total"]:
        print(
            "FAIL: collect mismatch\n"
            f" expected={manifest['expected_collect_total']}\n actual={dict(actual)}"
        )
        return 1
    stale = [e["id"] for e in deferred if e["id"] not in set(ids)]
    if stale:
        print(f"FAIL: deferred ids not collected: {stale}")
        return 1
    deferred_ids = {e["id"] for e in deferred}
    expected_ran = sum(manifest["expected_ran"].values())
    if len(ids) - len(deferred_ids) != expected_ran or expected_ran < 1:
        print("FAIL: expected_ran inconsistent with collected - deferred")
        return 1

    deselect = [f"--deselect={i}" for i in sorted(deferred_ids)]
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        run = _pytest("-q", "-rsxX", f"--junitxml={junit}", *deselect, *dirs)
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
        f"stage1a-core tests: OK ({len(ids)} collected, {len(deferred_ids)} deferred with "
        f"reasons, {ran} ran, 0 skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
