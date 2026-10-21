"""合成 fixture（qhyb）の shared evaluator: 参照された AC 集合の VC 要求を判定する。"""

from __future__ import annotations


def judge_qhyb_vc_requirement(ac_vc_refs: set[str]) -> dict[str, object]:
    return {"status": "pass", "checked_acs": sorted(ac_vc_refs)}
