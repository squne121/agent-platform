"""合成 fixture（qneg）の古い evaluator 実装（decoy）: consumer は import しない。同名 symbol を持つ。"""

from __future__ import annotations


def judge_qneg_vc_requirement(ac_vc_commands: dict[str, str]) -> dict[str, object]:
    missing = sorted(ac for ac, command in ac_vc_commands.items() if "git diff" not in command)
    return {"status": "fail" if missing else "pass", "missing_git_diff": missing}
