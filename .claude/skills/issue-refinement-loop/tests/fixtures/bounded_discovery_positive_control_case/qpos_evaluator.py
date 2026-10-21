"""合成 fixture（qpos）の shared evaluator: 参照された AC の VC command 本文が git diff を含むかを判定する。"""

from __future__ import annotations


def judge_qpos_vc_requirement(ac_vc_commands: dict[str, str]) -> dict[str, object]:
    missing = sorted(ac for ac, command in ac_vc_commands.items() if "git diff" not in command)
    return {"status": "fail" if missing else "pass", "missing_git_diff": missing}
