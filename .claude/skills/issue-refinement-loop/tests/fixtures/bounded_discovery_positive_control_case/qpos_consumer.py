"""合成 fixture（qpos）の consumer: 参照 AC の VC command 本文を shared evaluator へ渡して decision を返す。"""

from __future__ import annotations

from qpos_evaluator import judge_qpos_vc_requirement
from qpos_producer import collect_qpos_vc_rows


def route_qpos_vc_decision(issue_body: str, referenced_acs: set[str]) -> dict[str, object]:
    rows = collect_qpos_vc_rows(issue_body)
    ac_vc_commands = {row["ac"]: row["command"] for row in rows if row["ac"] in referenced_acs}
    return judge_qpos_vc_requirement(ac_vc_commands)
