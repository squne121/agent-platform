"""Fail when a migrated script imports a module that is neither stdlib, an installed
dependency, nor a file in the migrated closure (missing-import detection, AC1/AC4).

Every unresolved import must be listed in the Stage 1a manifest `deferred_imports`
with a reason; an unlisted miss or a stale (no longer missing) entry is a FAIL.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore", SyntaxWarning)  # legacy escapes in migrated sources
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "stage1a-core-manifest.json"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def unresolved(root: Path, scan_dirs: list[str], closure_dirs: list[str]) -> dict[str, list[str]]:
    """Map missing module -> sorted importing files (relative to root)."""
    local = {p.stem for d in closure_dirs for p in (root / d).glob("*.py")}
    missing: dict[str, set[str]] = {}
    for d in scan_dirs:
        for path in sorted((root / d).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for mod in imported_modules(path):
                if mod in sys.stdlib_module_names or mod in local:
                    continue
                if importlib.util.find_spec(mod) is not None:
                    continue
                missing.setdefault(mod, set()).add(str(path.relative_to(root)))
    return {m: sorted(files) for m, files in sorted(missing.items())}


_SCRIPT_LITERAL = re.compile(r"[\w\-]+\.py")


def unresolved_script_refs(
    root: Path, scan_dirs: list[str], closure_dirs: list[str]
) -> dict[str, list[str]]:
    """Map referenced `<name>.py` string literals (spec_from_file_location / subprocess
    targets) that exist in no closure dir -> sorted referencing files."""
    local = {p.name for d in closure_dirs for p in (root / d).glob("*.py")}
    missing: dict[str, set[str]] = {}
    for d in scan_dirs:
        for path in sorted((root / d).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _SCRIPT_LITERAL.fullmatch(node.value)
                    and node.value not in local
                ):
                    missing.setdefault(node.value, set()).add(str(path.relative_to(root)))
    return {m: sorted(files) for m, files in sorted(missing.items())}


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cfg = manifest["import_closure"]
    deferred = {e["module"] for e in cfg["deferred_imports"]}
    if any(not e.get("reason") for e in cfg["deferred_imports"]):
        print("FAIL: deferred import without reason")
        return 1
    missing = unresolved(ROOT, cfg["scan_dirs"], cfg["closure_dirs"])
    bad = False
    for mod, files in missing.items():
        if mod not in deferred:
            print(f"FAIL: unresolved import {mod!r} in {files}")
            bad = True
    for mod in sorted(deferred - set(missing)):
        print(f"FAIL: stale deferred_imports entry {mod!r} (now resolvable or unused)")
        bad = True
    refs = cfg["deferred_script_refs"]
    if any(not e.get("reason") for e in refs):
        print("FAIL: deferred script ref without reason")
        return 1
    listed = {e["script"] for e in refs}
    missing_refs = unresolved_script_refs(ROOT, cfg["scan_dirs"], cfg["closure_dirs"])
    for name, files in missing_refs.items():
        if name not in listed:
            print(f"FAIL: unresolved script reference {name!r} in {files}")
            bad = True
    for name in sorted(listed - set(missing_refs)):
        print(f"FAIL: stale deferred_script_refs entry {name!r}")
        bad = True
    if bad:
        return 1
    print(f"import closure: OK ({len(deferred)} imports, {len(listed)} script refs deferred)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
