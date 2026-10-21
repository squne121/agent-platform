"""合成 fixture（positive control）の shared evaluator: 参照 AC の VC command 本文が git diff を含むか判定する。"""

from __future__ import annotations


def evaluate_vc_requirement(ac_vc_commands: dict[str, str]) -> dict[str, object]:
    missing = sorted(ac for ac, command in ac_vc_commands.items() if "git diff" not in command)
    return {"status": "fail" if missing else "pass", "missing_git_diff": missing}
