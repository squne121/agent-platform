"""合成 fixture（positive control）の consumer: 参照 AC の VC command 本文を evaluator へ渡す。"""

from __future__ import annotations

from positive_control_case_evaluator import evaluate_vc_requirement
from positive_control_case_producer import produce_vc_records


def decide_vc_requirement(issue_body: str, referenced_acs: set[str]) -> dict[str, object]:
    records = produce_vc_records(issue_body)
    ac_vc_commands = {record["ac"]: record["command"] for record in records if record["ac"] in referenced_acs}
    return evaluate_vc_requirement(ac_vc_commands)
