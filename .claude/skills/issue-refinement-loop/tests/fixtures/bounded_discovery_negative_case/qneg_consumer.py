"""合成 fixture（qneg）の consumer: 参照 AC の VC 要求を shared evaluator へ渡して decision を返す。"""

from __future__ import annotations

from qneg_evaluator import judge_qneg_vc_requirement
from qneg_producer import collect_qneg_vc_rows


def route_qneg_vc_decision(issue_body: str, referenced_acs: set[str]) -> dict[str, object]:
    rows = collect_qneg_vc_rows(issue_body)
    ac_vc_refs = {row["ac"] for row in rows if row["ac"] in referenced_acs}
    return judge_qneg_vc_requirement(ac_vc_refs)
