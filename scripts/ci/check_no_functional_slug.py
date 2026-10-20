"""Fail when a legacy repository identity is hard-coded.

usage: check_no_functional_slug.py ROOT [ROOT ...]   (ROOT = directory or file)

exit 0 = clean, 1 = violation found, 2 = usage error / root missing / nothing scanned /
a target could not be read (an unreadable file is never reported as clean).

Scope: `.py` files are scanned through the AST (string constants only, so comments
are ignored); `.json .yml .yaml .toml .sh` are scanned as text. Markdown is exempt
(documentation may name the legacy repository); see docs/dev/stage0-foundation.md.
Absolute home paths are covered by scripts/check_public_hygiene.sh, not here.
"""

from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path

# Lines carrying this marker are intentional fixtures/definitions and are skipped.
ALLOW_MARKER = "allow-legacy-slug"
FORBIDDEN = (
    "squne121" + "/loop-protocol",  # allow-legacy-slug
    "refs/" + "loop-protocol/",  # allow-legacy-slug
)
TEXT_SUFFIXES = {".json", ".yml", ".yaml", ".toml", ".sh"}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
# The marker is honoured only in the checker itself and in its own test fixtures.
MARKER_ALLOWED = ("/scripts/ci/check_no_functional_slug.py", "/tests/stage0/")


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if not path.is_file() or SKIP_DIRS & set(parts):
            continue
        if any(a == ".claude" and b == "worktrees" for a, b in itertools.pairwise(parts)):
            continue
        yield path


def _folded_str(node: ast.AST) -> str | None:
    """String value of a constant or of a `+` concatenation of constants, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded_str(node.left), _folded_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _scan(path: Path) -> list[str]:
    """Violations in `path`; raises OSError/UnicodeDecodeError when it cannot be read."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [f"{path}: unparsable python"]
        found = []
        lines = text.splitlines()
        marker_ok = any(a in "/" + path.resolve().as_posix() for a in MARKER_ALLOWED)
        for node in ast.walk(tree):
            value = _folded_str(node)
            if value is not None and not (marker_ok and ALLOW_MARKER in lines[node.lineno - 1]):
                for needle in FORBIDDEN:
                    if needle in value.casefold():
                        found.append(f"{path}:{node.lineno}: forbidden {needle!r}")
        return found
    if path.suffix.lower() in TEXT_SUFFIXES:
        return [
            f"{path}:{n}: forbidden {needle!r}"
            for n, line in enumerate(text.splitlines(), 1)
            for needle in FORBIDDEN
            if needle in line.casefold()
        ]
    return []


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_no_functional_slug.py ROOT [ROOT ...]", file=sys.stderr)
        return 2
    scanned = 0
    violations: list[str] = []
    unreadable: list[str] = []
    for arg in argv:
        root = Path(arg)
        if not root.exists():
            print(f"root not found: {arg}", file=sys.stderr)
            return 2
        for path in _iter_files(root):
            if path.suffix.lower() == ".py" or path.suffix.lower() in TEXT_SUFFIXES:
                scanned += 1
                try:
                    violations.extend(_scan(path))
                except (OSError, UnicodeDecodeError) as exc:
                    unreadable.append(f"{path}: unreadable ({type(exc).__name__})")
    if scanned == 0:
        print("no scannable files", file=sys.stderr)
        return 2
    if unreadable:
        for u in unreadable:
            print(u, file=sys.stderr)
        return 2
    for v in violations:
        print(v)
    if violations:
        return 1
    print(f"no-functional-slug: OK ({scanned} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
