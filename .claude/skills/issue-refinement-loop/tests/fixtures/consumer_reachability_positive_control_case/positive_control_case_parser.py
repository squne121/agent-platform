"""合成 fixture（positive_control）の parser: Issue 本文の VC 行を AC marker と command 本文へ分解する。"""

from __future__ import annotations

import re

_AC_MARKER = re.compile(r"^#\s*(AC\d+)\s*$")
_COMMAND_LINE = re.compile(r"^\$\s+(.+)$")


def parse_ac_marker(line: str) -> str | None:
    match = _AC_MARKER.match(line.strip())
    return match.group(1) if match else None


def parse_vc_command(line: str) -> str | None:
    match = _COMMAND_LINE.match(line.strip())
    return match.group(1) if match else None
