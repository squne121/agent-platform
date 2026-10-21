"""合成 fixture（qhyb）の parser: Issue 本文の VC 行を AC marker と command 本文へ分解する。"""

from __future__ import annotations

import re

_AC_MARKER = re.compile(r"^#\s*(AC\d+)\s*$")
_COMMAND_LINE = re.compile(r"^\$\s+(.+)$")


def split_qhyb_vc_line(line: str) -> tuple[str | None, str | None]:
    text = line.strip()
    marker = _AC_MARKER.match(text)
    if marker:
        return marker.group(1), None
    command = _COMMAND_LINE.match(text)
    return None, (command.group(1) if command else None)
