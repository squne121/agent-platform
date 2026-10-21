"""合成 fixture（positive_control）の producer: Issue 本文から AC ごとの VC record を生成する。"""

from __future__ import annotations

from positive_control_case_parser import parse_ac_marker, parse_vc_command


def produce_vc_records(issue_body: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current_ac: str | None = None
    for line in issue_body.splitlines():
        marker = parse_ac_marker(line)
        if marker is not None:
            current_ac = marker
            continue
        command = parse_vc_command(line)
        if command is not None and current_ac is not None:
            records.append({"ac": current_ac, "command": command})
    return records
