"""Issue #2963 AC8: reachability runtime evaluator の hermetic 単体 test。

実 Claude Code process は起動しない。stream-json の stdout 行そのもの（runtime と同じ行形式）を
合成して「AC8 判定規則」の各分類を検証する。各 negative control は同じ builder の正常系と
ちょうど 1 点だけ異なる。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_HELPER_NAME = "issue2963_reachability_runtime_evaluator"
_HELPER_PATH = Path(__file__).resolve().parent / "issue2963_reachability_runtime_evaluator.py"


def _load_helper() -> Any:
    cached = sys.modules.get(_HELPER_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_HELPER_NAME, _HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_HELPER_NAME] = module
    spec.loader.exec_module(module)
    return module


EVAL = _load_helper()

ROOT = "/synthetic/repo-root"
INV = f"{ROOT}/artifacts/invocation-negative"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AGENT = "issue-design-reviewer"
# Native Claude Code wire key for the session (hook stdin payload and outer stream event); assembled so
# the literal never appears in a tracked file (public hygiene).
NATIVE_SESSION_KEY = "session" + "_id"

PARENT = "toolu_parent_1"
PATHS = {
    "producer": "fixtures/synthetic_case/synthetic_producer.py",
    "parser": "fixtures/synthetic_case/synthetic_parser.py",
    "evaluator": "fixtures/synthetic_case/synthetic_evaluator.py",
    "consumer": "fixtures/synthetic_case/synthetic_consumer.py",
}


class Stream:
    """stream-json 行を組み立てる hermetic builder（`extract_claude_*` helper が読む実形状）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.n = 0

    def add(self, obj: dict[str, Any]) -> "Stream":
        self.lines.append(json.dumps(obj))
        return self

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def init(self) -> "Stream":
        return self.add({"type": "system", "subtype": "init", NATIVE_SESSION_KEY: "s"})

    def agent_call(self, tool_use_id: str = PARENT, subagent_type: str = AGENT) -> "Stream":
        return self.add(
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Agent",
                            "input": {"subagent_type": subagent_type, "prompt": "p"},
                        }
                    ]
                },
            }
        )

    def agent_result(self, tool_use_id: str = PARENT, *, is_error: bool = False) -> "Stream":
        return self.add(
            {
                "type": "user",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "is_error": is_error,
                            "content": [{"type": "text", "text": "done"}],
                        }
                    ]
                },
            }
        )

    def _hook(self, hook_event: str, agent_id: str, agent_type: str) -> "Stream":
        payload = json.dumps({NATIVE_SESSION_KEY: "s", "hook_event_name": hook_event, "agent_id": agent_id, "agent_type": agent_type})
        return self.add(
            {
                "type": "system",
                "subtype": "hook_response",
                "hook_event": hook_event,
                "hook_name": f"{hook_event}:{agent_type}",
                "stdout": payload,
                "output": payload,
                NATIVE_SESSION_KEY: "s",
            }
        )

    def _hook_line(self, subtype: str, hook_event: str, hook_name: str, hook_id: str, payload: str = "") -> "Stream":
        line: dict[str, Any] = {
            "type": "system",
            "subtype": subtype,
            "hook_id": hook_id,
            "hook_name": hook_name,
            "hook_event": hook_event,
        }
        if subtype == "hook_response":
            line.update({"output": payload, "stdout": payload, "stderr": "", "exit_code": 0, "outcome": "success"})
        return self.add(line)

    def _real_lifecycle(self, hook_event: str, agent_id: str, agent_type: str, hooks: int, name: str) -> "Stream":
        """実 Claude Code 2.1.291 の record 形状: 1 invocation で configured hook の数だけ hook_started /
        hook_response が出る。`agent_id` / `agent_type` を持つのは hook stdin を echo する 1 本の response だけ。"""
        payload = json.dumps({NATIVE_SESSION_KEY: "s", "hook_event_name": hook_event, "agent_id": agent_id, "agent_type": agent_type})
        ids = [f"{hook_event}-{agent_id}-hook{n}" for n in range(hooks)]
        for hook_id in ids:
            self._hook_line("hook_started", hook_event, name, hook_id)
        for position, hook_id in enumerate(reversed(ids)):
            self._hook_line("hook_response", hook_event, name, hook_id, payload if position == 0 else "")
        return self

    def start(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._real_lifecycle("SubagentStart", agent_id, agent_type, 2, f"SubagentStart:{agent_type}")

    def stop(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._real_lifecycle("SubagentStop", agent_id, agent_type, 3, "SubagentStop")

    def start_single(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        """echo record だけの最小形（hook_started / 他 hook の response を持たない）。"""
        return self._hook("SubagentStart", agent_id, agent_type)

    def stop_single(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._hook("SubagentStop", agent_id, agent_type)

    def tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        output: str = "",
        *,
        tool_use_id: str | None = None,
        parent: str | None = PARENT,
        is_error: bool = False,
        with_result: bool = True,
    ) -> "Stream":
        self.n += 1
        tool_use_id = tool_use_id or f"toolu_child_{self.n}"
        self.add(
            {
                "type": "assistant",
                "parent_tool_use_id": parent,
                "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]},
            }
        )
        if with_result:
            self.add(
                {
                    "type": "user",
                    "parent_tool_use_id": parent,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "is_error": is_error,
                                "content": [{"type": "text", "text": output}],
                            }
                        ]
                    },
                }
            )
        return self

    def root(self, **kw: Any) -> "Stream":
        return self.tool(
            "Bash", {"command": f"git -C {INV} rev-parse --show-toplevel"}, kw.pop("output", ROOT + "\n"), **kw
        )

    def head(self, **kw: Any) -> "Stream":
        return self.tool("Bash", {"command": f"git -C {ROOT} rev-parse HEAD"}, kw.pop("output", HEAD + "\n"), **kw)

    def read(self, role: str, **kw: Any) -> "Stream":
        return self.tool("Read", {"file_path": f"{ROOT}/{PATHS[role]}"}, kw.pop("output", "source"), **kw)

    def read_sources(self, *, skip: tuple[str, ...] = ()) -> "Stream":
        """producer / parser / evaluator / consumer の 4 役の Read（`skip` で一部を欠落させる）。"""
        for role in PATHS:
            if role not in skip:
                self.read(role)
        return self

    def handback(self, message: str) -> "Stream":
        return self.tool("SubagentHandback", {"message": message}, "ok", tool_use_id="toolu_hb")

    def final(self, denials: list[dict[str, Any]] | None = None) -> "Stream":
        return self.add({"type": "result", "subtype": "success", "permission_denials": denials or []})


def normal_stream(kind: str = "negative", result: Any = None, **overrides: Any) -> Stream:
    """正常系: lifecycle 1 組 + 必須 tool 観測がすべて成功 + hand-back。"""
    s = Stream().init().agent_call().start()
    if kind != "simple":
        s.root().head().read_sources()
    else:
        s.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")
    s.handback(json.dumps(result if result is not None else default_result(kind)))
    s.stop().agent_result().final()
    return s


def default_result(kind: str) -> dict[str, Any]:
    if kind == "negative":
        return {
            "assessment": "findings",
            "findings": [
                {
                    "severity": "high",
                    "summary": "consumer が AC 番号集合だけを evaluator へ渡す",
                    "evidence_refs": [
                        f"HEAD {HEAD}",
                        f"{ROOT}/{PATHS['producer']}:7",
                        f"./{PATHS['parser']}",
                        f"{PATHS['evaluator']}:5",
                        f"{PATHS['consumer']}:12 decide_vc_requirement",
                    ],
                    "recommended_fix": "配線を拡張する",
                    "requires_owner_choice": True,
                }
            ],
        }
    return {"assessment": "clear", "findings": []}


def evaluate(stream: Stream | str, kind: str = "negative", result: Any = "__default__", **kw: Any) -> dict[str, Any]:
    stdout = stream if isinstance(stream, str) else stream.text()
    raw = default_result(kind) if result == "__default__" else result
    params: dict[str, Any] = {
        "stdout": stdout,
        "tested_head": HEAD,
        "resolved_root": ROOT,
        "invocation_dir": INV,
        "fixture_kind": kind,
        "fixture_paths": None if kind == "simple" else PATHS,
        "raw_result": raw,
    }
    params.update(kw)
    return EVAL.evaluate_reachability_runtime(**params)


def assert_outcome(outcome: dict[str, Any], verdict: str, rule: int) -> None:
    assert (outcome["verdict"], outcome["rule"]) == (verdict, rule), outcome
    assert outcome["exit_code"] == EVAL.EXIT_CODES[verdict]


# --- 正常系 -------------------------------------------------------------------------------------------------


def test_normal_cases_pass_for_negative_positive_and_simple() -> None:
    assert_outcome(evaluate(normal_stream("negative")), "pass", 5)
    assert_outcome(evaluate(normal_stream("positive"), "positive"), "pass", 5)
    assert_outcome(evaluate(normal_stream("simple"), "simple"), "pass", 5)


def test_lifecycle_rule_records_agent_id_and_stream_indexes_in_evidence() -> None:
    outcome = evaluate(normal_stream("negative"))
    lifecycle = outcome["evidence"]["lifecycle"]
    assert lifecycle["agent_id"] == "agent-1"
    assert lifecycle["start_stream_index"] < lifecycle["stop_stream_index"]
    assert all(outcome["evidence"]["required_observations"].values())


# --- 規則 1: stream なし --------------------------------------------------------------------------------------


def test_rule1_no_stream_or_unavailable_reason_is_unavailable() -> None:
    assert_outcome(evaluate(""), "unavailable", 1)
    assert_outcome(evaluate("not json\n"), "unavailable", 1)
    assert_outcome(evaluate(normal_stream(), claude_unavailable_reason="claude not found"), "unavailable", 1)


# --- 規則 2: permission 拒否 ----------------------------------------------------------------------------------


def _denial(tool_use_id: str | None, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    denial: dict[str, Any] = {"tool_name": name, "tool_input": tool_input}
    if tool_use_id is not None:
        denial["tool_use_id"] = tool_use_id
    return denial


def test_rule2_permission_denial_of_required_tool_is_unavailable_not_fail() -> None:
    s = Stream().init().agent_call().start()
    s.root(tool_use_id="toolu_root", is_error=True, output="permission denied by hook")
    s.stop().agent_result().final([_denial("toolu_root", "Bash", {})])
    assert_outcome(evaluate(s), "unavailable", 2)


def test_rule2_denial_without_tool_use_id_matches_on_tool_name_and_exact_input() -> None:
    command = {"command": f"git -C {INV} rev-parse --show-toplevel"}
    s = Stream().init().agent_call().start()
    s.root(is_error=True, output="blocked")
    s.stop().agent_result().final([_denial(None, "Bash", command)])
    assert_outcome(evaluate(s), "unavailable", 2)
    other = Stream().init().agent_call().start()
    other.root(is_error=True, output="blocked")
    other.stop().agent_result().final([_denial(None, "Bash", {"command": "different"})])
    assert_outcome(evaluate(other), "fail", 4)


def test_rule2_denial_of_parent_agent_tool_use_is_unavailable() -> None:
    s = Stream().init().agent_call().agent_result(is_error=True).final([_denial(PARENT, "Agent", {})])
    assert_outcome(evaluate(s), "unavailable", 2)


def test_rule2_uses_structured_denials_only_never_tool_result_wording() -> None:
    s = Stream().init().agent_call().start()
    s.root(is_error=True, output="Permission denied: requires approval (permission_denials)")
    s.stop().agent_result().final([])
    assert_outcome(evaluate(s), "fail", 4)


def test_rule2_precedes_lifecycle_rule() -> None:
    s = Stream().init().agent_call().agent_result(is_error=True).final([_denial(PARENT, "Agent", {})])
    assert "SubagentStart" not in s.text()
    assert_outcome(evaluate(s), "unavailable", 2)


# --- 規則 3: lifecycle / 区間 --------------------------------------------------------------------------------


def test_real_record_shape_one_invocation_yields_many_lifecycle_records_but_is_one_invocation() -> None:
    """実 stream の形状: 1 invocation の SubagentStart は 4 entry、SubagentStop は agent_type を持つのが 1 entry。"""
    stream = normal_stream("negative")
    lifecycle = EVAL.load_runner_module().extract_claude_hook_lifecycle_events(stream.text())
    starts = [e for e in lifecycle if e["hook_event"] == "SubagentStart"]
    stops = [e for e in lifecycle if e["hook_event"] == "SubagentStop"]
    assert len(starts) == 4 and {e["agent_type"] for e in starts} == {AGENT}
    assert len([e for e in starts if e["agent_id"]]) == 1
    assert len(stops) == 6 and len([e for e in stops if e["agent_type"] == AGENT]) == 1
    assert_outcome(evaluate(stream), "pass", 5)
    records = evaluate(stream)["lifecycle_records"]
    assert {r["subtype"] for r in records} == {"hook_started", "hook_response"}
    assert all(r["hook_id"] for r in records)
    assert sum(1 for r in records if r["agent_id"] == "agent-1" and r["hook_event"] == "SubagentStart") == 1


def test_rule3_single_echo_record_shape_is_still_one_invocation() -> None:
    s = Stream().init().agent_call().start_single()
    _with_tools_inline(s).stop_single().agent_result().final()
    assert_outcome(evaluate(s), "pass", 5)


def test_rule3_same_agent_id_echoed_by_two_observer_hooks_is_one_invocation() -> None:
    s = Stream().init().agent_call().start().start_single()
    _with_tools_inline(s).stop().agent_result().final()
    assert_outcome(evaluate(s), "pass", 5)


def test_rule3_distinct_agent_ids_are_distinct_invocations_even_with_real_record_shape() -> None:
    both = Stream().init().agent_call().start("agent-1").start("agent-2")
    _with_tools_inline(both).stop("agent-1").stop("agent-2").agent_result().final()
    assert_outcome(evaluate(both), "fail", 3)
    only_start = Stream().init().agent_call().start("agent-1").start("agent-2")
    _with_tools_inline(only_start).stop("agent-1").agent_result().final()
    assert_outcome(evaluate(only_start), "fail", 3)


def test_rule3_start_records_without_an_agent_id_echo_are_not_an_invocation() -> None:
    s = Stream().init().agent_call()
    for n in range(2):
        s._hook_line("hook_started", "SubagentStart", f"SubagentStart:{AGENT}", f"h{n}")
    _with_tools_inline(s).stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_stop_echo_without_reviewer_agent_type_is_missing_stop() -> None:
    s = Stream().init().agent_call().start()
    _with_tools_inline(s).stop(agent_type="other-agent").agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def _with_tools_inline(stream: Stream) -> Stream:
    return stream.root().head().read_sources()


def _with_tools(stream: Stream) -> Stream:
    return stream.root().head().read_sources()


def test_rule3_lifecycle_start_missing_is_fail() -> None:
    s = _with_tools(Stream().init().agent_call())
    s.stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_lifecycle_stop_missing_is_fail() -> None:
    s = _with_tools(Stream().init().agent_call().start())
    s.agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_wrong_agent_type_only_is_fail() -> None:
    s = _with_tools(Stream().init().agent_call().start(agent_type="codebase-investigator"))
    s.stop(agent_type="codebase-investigator").agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_duplicate_reviewer_lifecycle_is_fail() -> None:
    s = _with_tools(Stream().init().agent_call().start("agent-1"))
    s.stop("agent-1").start("agent-2").stop("agent-2").agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_stop_before_start_is_fail() -> None:
    s = Stream().init().agent_call().stop()
    _with_tools(s).start().agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_start_and_stop_with_different_agent_id_is_fail() -> None:
    s = _with_tools(Stream().init().agent_call().start("agent-1"))
    s.stop("agent-2").agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_contradictory_lifecycle_event_is_fail() -> None:
    s = Stream().init().agent_call()
    s.add(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "SubagentStart",
            "hook_name": f"SubagentStart:{AGENT}",
            "stdout": json.dumps({"agent_id": "a1", "agent_type": AGENT}),
            "output": json.dumps({"agent_id": "a2", "agent_type": AGENT}),
        }
    )
    _with_tools(s).stop("a1").agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_zero_tool_uses_inside_interval_is_fail() -> None:
    s = Stream().init().agent_call()
    s.root(parent="toolu_other_parent")  # 区間外・別 parent だけが存在する
    s.start().stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 3)


def test_rule3_parent_agent_tool_result_error_or_missing_is_fail() -> None:
    s = _with_tools(Stream().init().agent_call().start())
    s.stop().agent_result(is_error=True).final()
    assert_outcome(evaluate(s), "fail", 3)
    missing = _with_tools(Stream().init().agent_call().start())
    missing.stop().final()
    assert_outcome(evaluate(missing), "fail", 3)


def test_rule3_zero_or_two_parent_agent_tool_uses_is_fail() -> None:
    none = _with_tools(Stream().init().start())
    none.stop().final()
    assert_outcome(evaluate(none), "fail", 3)
    two = _with_tools(Stream().init().agent_call().agent_call("toolu_parent_2").start())
    two.stop().agent_result().agent_result("toolu_parent_2").final()
    assert_outcome(evaluate(two), "fail", 3)


# --- 規則 4: 必須 tool 観測 ---------------------------------------------------------------------------------


def test_rule4_tool_use_outside_interval_is_not_counted() -> None:
    s = Stream().init().agent_call()
    s.root().head()  # Start より前（区間外）
    s.start().read_sources()
    s.stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 4)
    after = Stream().init().agent_call().start().root().head().read_sources(skip=("consumer",))
    after.stop().read("consumer")  # Stop より後
    after.agent_result().final()
    assert_outcome(evaluate(after), "fail", 4)


def test_rule4_tool_use_of_other_parent_inside_interval_is_not_counted() -> None:
    s = Stream().init().agent_call().start()
    s.root().head().read_sources(skip=("consumer",))
    s.read("consumer", parent="toolu_other_parent")
    s.stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 4)


def test_rule4_non_permission_failures_are_fail_not_unavailable() -> None:
    root_failed = Stream().init().agent_call().start().root(is_error=True, output="fatal: not a git repository")
    root_failed.head().read_sources().stop().agent_result().final()
    assert_outcome(evaluate(root_failed), "fail", 4)
    head_failed = Stream().init().agent_call().start().root().head(is_error=True, output="fatal: bad revision")
    head_failed.read_sources().stop().agent_result().final()
    assert_outcome(evaluate(head_failed), "fail", 4)
    file_missing = Stream().init().agent_call().start().root().head().read_sources(skip=("consumer",))
    file_missing.read("consumer", is_error=True, output="File does not exist.").stop().agent_result().final()
    assert_outcome(evaluate(file_missing), "fail", 4)


def test_rule4_root_stdout_must_match_resolved_root_and_head_must_match_tested_head() -> None:
    wrong_root = Stream().init().agent_call().start().root(output="/somewhere/else\n")
    wrong_root.head().read_sources().stop().agent_result().final()
    assert_outcome(evaluate(wrong_root), "fail", 4)
    wrong_head = Stream().init().agent_call().start().root().head(output=OTHER_HEAD + "\n")
    wrong_head.read_sources().stop().agent_result().final()
    assert_outcome(evaluate(wrong_head), "fail", 4)


def test_rule4_only_one_of_four_source_reads_is_fail() -> None:
    s = Stream().init().agent_call().start().root().head().read("consumer")
    s.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    outcome = evaluate(s)
    assert_outcome(outcome, "fail", 4)
    for role in ("producer", "parser", "evaluator"):
        assert f"R-SRC:{role}" in outcome["reason"]
    assert "R-SRC:consumer" not in outcome["reason"]


def test_rule4_missing_only_the_evaluator_read_is_fail_even_when_everything_else_is_normal() -> None:
    """R-ROOT / R-HEAD / producer / parser / consumer は正常で evaluator の Read だけが欠落 -> 規則 4 の FAIL。"""
    assert "evaluator" in EVAL.SOURCE_ROLES and set(EVAL.SOURCE_ROLES) == set(PATHS)
    s = Stream().init().agent_call().start().root().head().read_sources(skip=("evaluator",))
    s.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    outcome = evaluate(s)
    assert_outcome(outcome, "fail", 4)
    observed = outcome["evidence"]["required_observations"]
    assert observed["R-SRC:evaluator"] is False
    assert [name for name, ok in observed.items() if not ok] == ["R-SRC:evaluator"]
    # 対照: 同じ stream に evaluator の Read を足すと PASS する（欠落だけが原因）。
    ok_stream = normal_stream("negative")
    assert_outcome(evaluate(ok_stream), "pass", 5)
    # evaluator の Read が permission 拒否以外で失敗した場合も規則 4 の FAIL。
    failed = Stream().init().agent_call().start().root().head().read_sources(skip=("evaluator",))
    failed.read("evaluator", is_error=True, output="File does not exist.")
    failed.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    assert_outcome(evaluate(failed), "fail", 4)


def test_rule4_commands_must_match_exact_git_c_token_sequences() -> None:
    cwd_relative = Stream().init().agent_call().start()
    cwd_relative.tool("Bash", {"command": "git rev-parse --show-toplevel"}, ROOT + "\n")
    cwd_relative.head().read_sources().stop().agent_result().final()
    assert_outcome(evaluate(cwd_relative), "fail", 4)
    wrong_dir = Stream().init().agent_call().start()
    wrong_dir.tool("Bash", {"command": f"git -C {ROOT}/other rev-parse --show-toplevel"}, ROOT + "\n")
    wrong_dir.head().read_sources().stop().agent_result().final()
    assert_outcome(evaluate(wrong_dir), "fail", 4)


def test_rule4_marker_string_only_is_fail_even_with_perfect_raw_result() -> None:
    s = Stream().init().agent_call().start()
    s.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")  # bundle だけ読み、source を観測していない
    s.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 4)


def test_rule4_simple_fixture_imposes_no_required_observation() -> None:
    assert_outcome(evaluate(normal_stream("simple"), "simple"), "pass", 5)


# --- 規則 5: verdict ----------------------------------------------------------------------------------------


def _finding(severity: str, refs: list[str]) -> dict[str, Any]:
    return {
        "severity": severity,
        "summary": "s",
        "evidence_refs": refs,
        "recommended_fix": "f",
        "requires_owner_choice": False,
    }


def test_rule5_negative_uses_union_of_high_blocker_evidence_refs() -> None:
    result = {
        "assessment": "findings",
        "findings": [
            _finding("high", [f"{PATHS['producer']}:1", f"HEAD={HEAD}"]),
            _finding("blocker", [f"{ROOT}/{PATHS['parser']}", PATHS["evaluator"]]),
            _finding("high", [f"./{PATHS['consumer']}:3"]),
        ],
    }
    assert_outcome(evaluate(normal_stream("negative", result), result=result), "pass", 5)


def test_rule5_negative_medium_low_refs_are_excluded_from_the_union() -> None:
    result = {
        "assessment": "findings",
        "findings": [
            _finding("high", [f"{PATHS['producer']}", f"{PATHS['parser']}", PATHS["evaluator"], HEAD]),
            _finding("medium", [f"{PATHS['consumer']}"]),
            _finding("low", [f"{PATHS['consumer']}"]),
        ],
    }
    assert_outcome(evaluate(normal_stream("negative", result), result=result), "fail", 5)


def test_rule5_negative_requires_exact_tested_head_sha_token() -> None:
    for refs_head in (OTHER_HEAD, HEAD[:39], HEAD[:-1] + "0", "no head here"):
        result = {
            "assessment": "findings",
            "findings": [_finding("high", [*PATHS.values(), refs_head])],
        }
        assert_outcome(evaluate(normal_stream("negative", result), result=result), "fail", 5)


def test_rule5_negative_evidence_union_lacking_the_evaluator_path_is_fail() -> None:
    """全 Read は正常でも、high|blocker evidence_refs の和集合が evaluator path を欠けば規則 5 の FAIL。"""
    without_evaluator = [*(path for role, path in PATHS.items() if role != "evaluator"), HEAD]
    result = {"assessment": "findings", "findings": [_finding("high", without_evaluator)]}
    assert_outcome(evaluate(normal_stream("negative", result), result=result), "fail", 5)
    # evaluator path が medium だけに現れる場合も和集合に含まれない。
    split = {
        "assessment": "findings",
        "findings": [_finding("high", without_evaluator), _finding("medium", [PATHS["evaluator"]])],
    }
    assert_outcome(evaluate(normal_stream("negative", split), result=split), "fail", 5)
    # 対照: evaluator path を high finding に含めれば PASS。
    full = {"assessment": "findings", "findings": [_finding("high", [*PATHS.values(), HEAD])]}
    assert_outcome(evaluate(normal_stream("negative", full), result=full), "pass", 5)


def test_rule5_negative_clear_or_only_medium_findings_is_fail() -> None:
    clear = {"assessment": "clear", "findings": []}
    assert_outcome(evaluate(normal_stream("negative", clear), result=clear), "fail", 5)
    medium = {"assessment": "findings", "findings": [_finding("medium", [*PATHS.values(), HEAD])]}
    assert_outcome(evaluate(normal_stream("negative", medium), result=medium), "fail", 5)


def test_rule5_unstructured_or_ambiguous_raw_result_is_fail() -> None:
    for raw in (None, "", "looks fine to me", {"assessment": "maybe", "findings": []}, {"assessment": "clear"}):
        assert_outcome(evaluate(normal_stream("negative"), result=raw), "fail", 5)
    malformed = {"assessment": "findings", "findings": [{"severity": "high", "evidence_refs": "not-a-list"}]}
    assert_outcome(evaluate(normal_stream("negative", malformed), result=malformed), "fail", 5)


def test_rule5_raw_result_text_with_code_fence_is_parsed_structurally() -> None:
    text = "結果です\n```json\n" + json.dumps(default_result("negative")) + "\n```\n"
    assert_outcome(evaluate(normal_stream("negative"), result=text), "pass", 5)


def test_rule5_positive_control_clear_or_non_citing_findings_pass_and_citing_high_fails() -> None:
    other = {"assessment": "findings", "findings": [_finding("high", ["docs/elsewhere.md:1"])]}
    assert_outcome(evaluate(normal_stream("positive", other), "positive", result=other), "pass", 5)
    citing = {"assessment": "findings", "findings": [_finding("high", [PATHS["consumer"]])]}
    assert_outcome(evaluate(normal_stream("positive", citing), "positive", result=citing), "fail", 5)
    citing_evaluator = {"assessment": "findings", "findings": [_finding("high", [PATHS["evaluator"]])]}
    stream = normal_stream("positive", citing_evaluator)
    assert_outcome(evaluate(stream, "positive", result=citing_evaluator), "fail", 5)
    medium_only = {"assessment": "findings", "findings": [_finding("medium", [PATHS["consumer"]])]}
    assert_outcome(evaluate(normal_stream("positive", medium_only), "positive", result=medium_only), "pass", 5)


def test_rule5_simple_with_high_or_blocker_finding_is_fail() -> None:
    for severity in ("high", "blocker"):
        over = {"assessment": "findings", "findings": [_finding(severity, ["x.md"])]}
        assert_outcome(evaluate(normal_stream("simple", over), "simple", result=over), "fail", 5)
    medium = {"assessment": "findings", "findings": [_finding("medium", ["x.md"])]}
    assert_outcome(evaluate(normal_stream("simple", medium), "simple", result=medium), "pass", 5)


def test_unknown_fixture_kind_is_fail_closed() -> None:
    assert_outcome(evaluate(normal_stream(), kind="bogus", fixture_paths=PATHS), "fail", 5)


# --- raw result 抽出 -----------------------------------------------------------------------------------------


def test_extract_reviewer_raw_result_reads_structured_handback_only() -> None:
    stream = normal_stream("negative")
    raw = EVAL.extract_reviewer_raw_result(stream.text())
    assert raw is not None and EVAL.parse_raw_result_object(raw) == default_result("negative")
    no_handback = Stream().init().agent_call().start().root().stop().agent_result().final()
    assert EVAL.extract_reviewer_raw_result(no_handback.text()) is None


@pytest.mark.parametrize("kind", ["negative", "positive", "simple"])
def test_pipeline_extracted_raw_result_feeds_the_evaluator(kind: str) -> None:
    stream = normal_stream(kind)
    raw = EVAL.extract_reviewer_raw_result(stream.text())
    assert_outcome(evaluate(stream, kind, result=raw), "pass", 5)


def test_rule4_real_deviation_compound_bash_and_cat_instead_of_read_is_fail() -> None:
    """実 reviewer の逸脱形状: root / HEAD を 1 本の複合 Bash（shell 変数・連結）で実行し、source を cat で読む。"""
    s = Stream().init().agent_call().start()
    s.tool(
        "Bash",
        {"command": f"D={INV}; cat $D/bundle.json; git -C $D rev-parse --show-toplevel; git -C $D rev-parse HEAD"},
        f"{{}}\n{ROOT}\n{HEAD}\n",
    )
    s.tool("Bash", {"command": f"cd {ROOT}/fixtures/synthetic_case/ && for f in *.py; do cat $f; done"}, "source")
    s.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    outcome = evaluate(s)
    assert_outcome(outcome, "fail", 4)
    assert all(not ok for ok in outcome["evidence"]["required_observations"].values())


def test_artifact_tool_use_summary_is_sanitized_and_diagnosable() -> None:
    home_path = "/ho" "me/someone/secret-project"
    s = Stream().init().agent_call().start()
    s.tool("Bash", {"command": f"cd {ROOT}/x && git -C {INV} log {home_path}"}, f"{ROOT}/x {home_path} " + "z" * 400)
    s.read("producer")
    s.tool("Grep", {"pattern": "secret-pattern"}, "hit")
    s.stop().agent_result().final()
    outcome = evaluate(s)
    records = {r["name"]: r for r in outcome["tool_use_records"]}
    bash = records["Bash"]
    assert bash["input_summary"].startswith("cd <ROOT>/x && git -C <INVOCATION_DIR> log <HOME>")
    assert len(bash["stdout_head"]) <= 200 and "/home/" not in bash["stdout_head"] and ROOT not in bash["stdout_head"]
    assert records["Read"]["input_summary"] == PATHS["producer"].join(["<ROOT>/", ""])
    assert records["Read"]["result_is_error"] is False
    assert "input_summary" not in records["Grep"], "inputs of other tools must not be retained"
    dumped = json.dumps(outcome["tool_use_records"])
    assert "/home/" not in dumped and ROOT not in dumped and "secret-pattern" not in dumped
