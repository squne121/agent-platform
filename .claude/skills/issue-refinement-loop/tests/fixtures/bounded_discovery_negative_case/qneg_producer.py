"""合成 fixture（qneg）の producer: Issue 本文から AC ごとの VC row を生成する。"""

from __future__ import annotations

from qneg_parser import split_qneg_vc_line


def collect_qneg_vc_rows(issue_body: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_ac: str | None = None
    for line in issue_body.splitlines():
        marker, command = split_qneg_vc_line(line)
        if marker is not None:
            current_ac = marker
        elif command is not None and current_ac is not None:
            rows.append({"ac": current_ac, "command": command})
    return rows
