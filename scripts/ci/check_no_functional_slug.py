"""Fail when a legacy repository identity is hard-coded.

usage: check_no_functional_slug.py ROOT [ROOT ...]   (ROOT = directory or file)

exit 0 = clean, 1 = violation found, 2 = usage error / root missing / nothing scanned.

Scope: `.py` files are scanned through the AST (string constants only, so comments
are ignored); `.json .yml .yaml .toml .sh` are scanned as text. Markdown is exempt
(documentation may name the legacy repository); see docs/dev/stage0-foundation.md.
Absolute home paths are covered by scripts/check_public_hygiene.sh, not here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Built by concatenation so this file does not trip its own scan.
FORBIDDEN = (
    "squne121" + "/loop-protocol",
    "refs/" + "loop-protocol/",
)
TEXT_SUFFIXES = {".json", ".yml", ".yaml", ".toml", ".sh"}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "worktrees"}


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not (SKIP_DIRS & set(path.relative_to(root).parts)):
            yield path


def _scan(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [f"{path}: unparsable python"]
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for needle in FORBIDDEN:
                    if needle in node.value:
                        found.append(f"{path}:{node.lineno}: forbidden {needle!r}")
        return found
    if path.suffix in TEXT_SUFFIXES:
        return [
            f"{path}:{n}: forbidden {needle!r}"
            for n, line in enumerate(text.splitlines(), 1)
            for needle in FORBIDDEN
            if needle in line
        ]
    return []


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_no_functional_slug.py ROOT [ROOT ...]", file=sys.stderr)
        return 2
    scanned = 0
    violations: list[str] = []
    for arg in argv:
        root = Path(arg)
        if not root.exists():
            print(f"root not found: {arg}", file=sys.stderr)
            return 2
        for path in _iter_files(root):
            if path.suffix == ".py" or path.suffix in TEXT_SUFFIXES:
                scanned += 1
                violations.extend(_scan(path))
    if scanned == 0:
        print("no scannable files", file=sys.stderr)
        return 2
    for v in violations:
        print(v)
    if violations:
        return 1
    print(f"no-functional-slug: OK ({scanned} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
