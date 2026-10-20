"""Run the Stage 0 pytest lane with false-green guards.

1. `pytest --co` must exit 0 (exit 5 = nothing collected is a FAIL).
2. Per-file collected counts must equal docs/dev/stage0-test-manifest.json exactly
   (unlisted test files and missing files are FAILs).
3. The real run must exit 0 and its JUnit XML must report 0 skipped tests.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "stage0-test-manifest.json"


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected: dict[str, int] = manifest["expected_collect"]
    dirs = manifest["test_dirs"]

    co = _pytest("--co", "-q", *dirs)
    if co.returncode != 0:
        print(co.stdout + co.stderr)
        print(f"FAIL: collect exit code {co.returncode} (5 = collected nothing)")
        return 1
    actual = Counter(m.group(1) for m in re.finditer(r"^(\S+?)::", co.stdout, re.MULTILINE))
    if dict(actual) != expected:
        print(f"FAIL: collect mismatch\n expected={expected}\n actual={dict(actual)}")
        return 1
    total = sum(actual.values())
    if total < 1:
        print("FAIL: collected 0 tests")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        run = _pytest("-q", "-rsxX", f"--junitxml={junit}", *dirs)
        print(run.stdout + run.stderr)
        if run.returncode != 0:
            print(f"FAIL: pytest exit code {run.returncode}")
            return 1
        suite = ET.parse(junit).getroot()
        suite = suite if suite.tag == "testsuite" else suite[0]
        skipped = int(suite.get("skipped", "0"))
        ran = int(suite.get("tests", "0"))
    if skipped != 0 or ran != total:
        print(f"FAIL: skipped={skipped} ran={ran} collected={total}")
        return 1
    print(f"stage0 tests: OK ({total} collected, {ran} ran, 0 skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
