"""合成 fixture（qhyb）の consumer: 参照 AC の VC 要求を shared evaluator へ渡して decision を返す。"""

from __future__ import annotations

from qhyb_evaluator import judge_qhyb_vc_requirement
from qhyb_producer import collect_qhyb_vc_rows


def route_qhyb_vc_decision(issue_body: str, referenced_acs: set[str]) -> dict[str, object]:
    rows = collect_qhyb_vc_rows(issue_body)
    ac_vc_refs = {row["ac"] for row in rows if row["ac"] in referenced_acs}
    return judge_qhyb_vc_requirement(ac_vc_refs)
