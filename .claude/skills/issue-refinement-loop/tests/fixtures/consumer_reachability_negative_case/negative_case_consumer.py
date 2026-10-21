"""合成 fixture（negative）の consumer: 参照 AC の VC 要求を shared evaluator へ渡して decision を返す。"""

from __future__ import annotations

from negative_case_evaluator import evaluate_vc_requirement
from negative_case_producer import produce_vc_records


def decide_vc_requirement(issue_body: str, referenced_acs: set[str]) -> dict[str, object]:
    records = produce_vc_records(issue_body)
    ac_vc_refs = {record["ac"] for record in records if record["ac"] in referenced_acs}
    return evaluate_vc_requirement(ac_vc_refs)
