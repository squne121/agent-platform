"""Issue #2973 AC7: bounded discovery pure evaluator の hermetic 単体 test。

実 Claude Code process は起動しない。stream-json の stdout 行そのもの（runtime と同じ行形式）を合成して、
`issue2973_bounded_discovery_runtime_evaluator.py` の各分類を検証する。各 negative control は同じ builder の
正常系とちょうど 1 点だけ異なり、期待する違反 code が出ることを確かめる（false-green 防止）。

discovery lane は 2 つ（専用 Grep / Glob と、root 束縛の Bash find / grep）。正常系と主要な negative control は
lane ごと（`grep` / `glob` / `bash_grep` / `bash_find`）に検証する。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> Any:
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _TESTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OLD = _load("issue2963_reachability_runtime_evaluator", "issue2963_reachability_runtime_evaluator.py")
NEW = _load("issue2973_bounded_discovery_runtime_evaluator", "issue2973_bounded_discovery_runtime_evaluator.py")

ROOT = "/synthetic/repo-root"
INV = f"{ROOT}/artifacts/invocation-x"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AGENT = "issue-design-reviewer"
PARENT = "toolu_parent_1"
SYN = "synthetic_fx"

LANES = ("grep", "glob", "bash_grep", "bash_find")


def _roles(listed: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    for role in ("producer", "parser", "evaluator", "consumer"):
        roles[role] = {
            "path": f"{SYN}/zz_{role}.py",
            "symbol": f"fn_{role}_zz",
            "file_fragment": f"zz_{role}",
            "listed": role in listed,
            "decoy_path": f"{SYN}/decoy/zz_stale_{role}.py" if role == "evaluator" else None,
        }
    return roles


NEG_ROLES = _roles()
HYB_ROLES = _roles(("producer", "parser"))
DECOY = f"{SYN}/decoy/zz_stale_evaluator.py"
ALLOWED = [f"{SYN}/glossary.md"]
_CONSUMER_TEXT = "1\tfrom zz_evaluator import fn_evaluator_zz\n2\tfrom zz_producer import fn_producer_zz\n"
_PRODUCER_TEXT = "1\tfrom zz_parser import fn_parser_zz\n"
_SOURCE_TEXT = {"consumer": _CONSUMER_TEXT, "producer": _PRODUCER_TEXT}
EVAL_SYM = NEG_ROLES["evaluator"]["symbol"]
EVAL_FRAG = NEG_ROLES["evaluator"]["file_fragment"]
EVAL_PATH = NEG_ROLES["evaluator"]["path"]


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

    def init(self, tools: list[str] | None = None) -> "Stream":
        event: dict[str, Any] = {"type": "system", "subtype": "init", "session_ref": "s"}
        if tools is not None:
            event["tools"] = tools
        return self.add(event)

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

    def _lifecycle(self, hook_event: str, agent_id: str, agent_type: str, hooks: int, name: str) -> "Stream":
        payload = json.dumps({"hook_event_name": hook_event, "agent_id": agent_id, "agent_type": agent_type})
        ids = [f"{hook_event}-{agent_id}-hook{n}" for n in range(hooks)]
        for hook_id in ids:
            self._hook_line("hook_started", hook_event, name, hook_id)
        for position, hook_id in enumerate(reversed(ids)):
            self._hook_line("hook_response", hook_event, name, hook_id, payload if position == 0 else "")
        return self

    def start(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._lifecycle("SubagentStart", agent_id, agent_type, 2, f"SubagentStart:{agent_type}")

    def stop(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._lifecycle("SubagentStop", agent_id, agent_type, 3, "SubagentStop")

    def tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        output: str = "",
        *,
        tool_use_id: str | None = None,
        parent: str | None = PARENT,
        is_error: bool = False,
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
        return self.add(
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

    def root(self, **kw: Any) -> "Stream":
        return self.tool(
            "Bash", {"command": f"git -C {INV} rev-parse --show-toplevel"}, kw.pop("output", ROOT + "\n"), **kw
        )

    def head(self, **kw: Any) -> "Stream":
        return self.tool("Bash", {"command": f"git -C {ROOT} rev-parse HEAD"}, kw.pop("output", HEAD + "\n"), **kw)

    def bundle(self) -> "Stream":
        self.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")
        return self.tool("Read", {"file_path": f"{INV}/body.md"}, "body")

    def grep(self, pattern: str, output: str, path: str | None = ROOT, **kw: Any) -> "Stream":
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        return self.tool("Grep", tool_input, output, **kw)

    def glob(self, pattern: str, output: str, path: str | None = ROOT, **kw: Any) -> "Stream":
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        return self.tool("Glob", tool_input, output, **kw)

    def bash(self, command: str, output: str = "", **kw: Any) -> "Stream":
        return self.tool("Bash", {"command": command}, output, **kw)

    def bash_grep(self, pattern: str, output: str, path: str = ROOT, flags: str = "-rl", **kw: Any) -> "Stream":
        return self.bash(f"grep {flags} -E '{pattern}' {path}", output, **kw)

    def bash_find(self, fragment: str, output: str, path: str = ROOT, **kw: Any) -> "Stream":
        return self.bash(f"find {path} -type f -name '{fragment}*'", output, **kw)

    def read_role(self, roles: dict[str, dict[str, Any]], role: str, **kw: Any) -> "Stream":
        output = kw.pop("output", _SOURCE_TEXT.get(role, "source"))
        return self.tool("Read", {"file_path": f"{ROOT}/{roles[role]['path']}"}, output, **kw)

    def read_path(self, rel: str, output: str = "source", **kw: Any) -> "Stream":
        return self.tool("Read", {"file_path": f"{ROOT}/{rel}"}, output, **kw)

    def handback(self, message: str) -> "Stream":
        return self.tool("SubagentHandback", {"message": message}, "ok", tool_use_id="toolu_hb")

    def final(self, denials: list[dict[str, Any]] | None = None, subtype: str = "success") -> "Stream":
        return self.add({"type": "result", "subtype": subtype, "permission_denials": denials or []})


def _hit_text(*rels: str, extra: str = "") -> str:
    return "Found files\n" + "\n".join(rels) + extra


def _abs_lines(*rels: str, extra: str = "") -> str:
    return "\n".join(f"{ROOT}/{rel}" for rel in rels) + extra


def _all_symbols_pattern(roles: dict[str, dict[str, Any]], only_unlisted: bool = True) -> str:
    return "|".join(r["symbol"] for r in roles.values() if not (only_unlisted and r["listed"]))


def _finding(severity: str, refs: list[str]) -> dict[str, Any]:
    return {
        "severity": severity,
        "summary": "s",
        "evidence_refs": refs,
        "recommended_fix": "f",
        "requires_owner_choice": False,
    }


def gap_result(roles: dict[str, dict[str, Any]], head: str = HEAD) -> dict[str, Any]:
    refs = [f"HEAD {head}", *(f"{ROOT}/{r['path']}:5" for r in roles.values())]
    return {"assessment": "findings", "findings": [_finding("high", refs)]}


CLEAR = {"assessment": "clear", "findings": []}


def default_result(kind: str) -> dict[str, Any]:
    if kind in ("negative", "hybrid"):
        return gap_result(NEG_ROLES if kind == "negative" else HYB_ROLES)
    return CLEAR


def do_search(s: Stream, lane: str, roles: dict[str, dict[str, Any]], hits: list[str], **kw: Any) -> Stream:
    """`lane` の検索を発行する。`hits` は結果に現れる repository 相対 path（decoy / 自己参照を含めてよい）。"""
    unresolved = [r for r in roles.values() if not r["listed"]]
    self_ref = f"\n{SYN}/test_self.py:3:  '{EVAL_PATH}'"
    if lane == "grep":
        s.grep("|".join(r["symbol"] for r in unresolved), _hit_text(*hits, extra=self_ref), **kw)
    elif lane == "bash_grep":
        s.bash_grep("|".join(r["symbol"] for r in unresolved), _abs_lines(*hits, extra=self_ref), **kw)
    elif lane == "glob":
        for role in unresolved:
            mine = [p for p in hits if Path(p).stem == role["file_fragment"]]
            s.glob(f"**/{role['file_fragment']}*", _hit_text(*mine), **kw)
    elif lane == "bash_find":
        names = " -o -name ".join(f"'{r['file_fragment']}*'" for r in unresolved)
        s.bash(f"find {ROOT} -type f -name {names}", _abs_lines(*hits), **kw)
    else:  # pragma: no cover
        raise AssertionError(lane)
    return s


def discovery_steps(s: Stream, kind: str, lane: str = "grep") -> Stream:
    """正常系の observation 部分（root -> HEAD -> bundle -> discovery -> Read）。"""
    s.root().head().bundle()
    if kind in ("negative", "positive"):
        roles = NEG_ROLES
        do_search(s, lane, roles, [*(r["path"] for r in roles.values()), DECOY])
        s.read_role(roles, "consumer")
        if kind == "negative":
            s.read_path(DECOY, "decoy source")  # 同名 symbol の decoy（bound 内。target 確定とは別）
        for role in ("evaluator", "producer", "parser"):
            s.read_role(roles, role)
    elif kind == "hybrid":
        roles = HYB_ROLES
        s.read_role(roles, "producer")
        s.read_role(roles, "parser")
        do_search(s, lane, roles, [roles["evaluator"]["path"], roles["consumer"]["path"]])
        s.read_role(roles, "consumer")
        s.read_role(roles, "evaluator")
    return s


def normal_stream(kind: str = "negative", result: Any = "__default__", lane: str = "grep") -> Stream:
    s = Stream().init().agent_call().start()
    if kind == "simple":
        s.bundle().read_path(ALLOWED[0], "glossary")
    else:
        discovery_steps(s, kind, lane)
    raw = default_result(kind) if result == "__default__" else result
    s.handback(json.dumps(raw))
    return s.stop().agent_result().final()


def evaluate(stream: Stream | str, kind: str = "negative", result: Any = "__default__", **kw: Any) -> dict[str, Any]:
    stdout = stream if isinstance(stream, str) else stream.text()
    raw = default_result(kind) if result == "__default__" else result
    roles = {"negative": NEG_ROLES, "positive": NEG_ROLES, "hybrid": HYB_ROLES, "simple": None}[kind]
    params: dict[str, Any] = {
        "stdout": stdout,
        "tested_head": HEAD,
        "resolved_root": ROOT,
        "invocation_dir": INV,
        "fixture_kind": kind,
        "fixture_roles": roles,
        "allowed_read_paths": ALLOWED if kind == "simple" else [],
        "raw_result": raw,
    }
    params.update(kw)
    return NEW.evaluate_bounded_discovery_runtime(**params)


def assert_outcome(outcome: dict[str, Any], verdict: str, rule: int, violation: str | None = None) -> None:
    assert (outcome["verdict"], outcome["rule"]) == (verdict, rule), outcome
    assert outcome["exit_code"] == NEW.EXIT_CODES[verdict]
    if violation is not None:
        violations = outcome["evidence"]["violations"]
        assert any(v.startswith(violation) for v in violations), (violation, violations)


def build(kind: str, mutate: Any, tools: list[str] | None = None) -> Stream:
    """正常系の observation を `mutate(stream)` で置き換えた stream（Start / Stop / handback は共通）。"""
    s = Stream().init(tools).agent_call().start()
    mutate(s)
    s.handback(json.dumps(default_result(kind)))
    return s.stop().agent_result().final()


def _read_all_targets(s: Stream, roles: dict[str, dict[str, Any]] = NEG_ROLES) -> None:
    for role in ("consumer", "evaluator", "producer", "parser"):
        s.read_role(roles, role)


def _prefix(s: Stream) -> None:
    s.root().head().bundle()


# --- 正常系（false-green でない: 各 negative control の対照） ---------------------------------------------------


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("kind", ["negative", "positive", "hybrid"])
def test_normal_cases_pass_in_every_discovery_lane(kind: str, lane: str) -> None:
    outcome = evaluate(normal_stream(kind, lane=lane), kind)
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["violations"] == []
    expected_lane = "dedicated" if lane in ("grep", "glob") else "bash"
    assert outcome["evidence"]["discovery"]["lanes_used"] == [expected_lane]


def test_simple_normal_case_passes() -> None:
    outcome = evaluate(normal_stream("simple"), "simple")
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["violations"] == []


def test_constants_are_the_fixed_issue_contract_values() -> None:
    assert NEW.DISCOVERY_SEARCH_CALL_MAX == 8
    assert NEW.DISCOVERY_SOURCE_READ_MAX == 8
    assert NEW.SEARCH_SCOPE == "repository_root_only"
    assert NEW.DISCOVERY_TOOLS == ("Grep", "Glob")
    assert NEW.DISCOVERY_BASH_LANE == ("find", "grep")


@pytest.mark.parametrize("lane", LANES)
def test_normal_hybrid_does_not_search_for_listed_roles_and_negative_resolves_all_four(lane: str) -> None:
    hybrid = evaluate(normal_stream("hybrid", lane=lane), "hybrid")
    roles = hybrid["evidence"]["discovery"]["roles"]
    assert roles["producer"]["via"] == "direct_read" and roles["parser"]["via"] == "direct_read"
    assert roles["evaluator"]["via"] == "discovery" and roles["consumer"]["via"] == "discovery"
    negative = evaluate(normal_stream("negative", lane=lane))
    assert all(r["satisfied"] and not r["listed"] for r in negative["evidence"]["discovery"]["roles"].values())


def test_mixed_lanes_pass_when_each_search_is_eligible() -> None:
    def mixed(s: Stream) -> None:
        _prefix(s)
        s.grep(
            NEG_ROLES["consumer"]["symbol"],
            _hit_text(NEG_ROLES["consumer"]["path"], NEG_ROLES["producer"]["path"]),
        )
        s.bash_find(EVAL_FRAG, _abs_lines(EVAL_PATH, DECOY))
        s.bash_grep(NEG_ROLES["parser"]["symbol"], _abs_lines(NEG_ROLES["parser"]["path"]))
        _read_all_targets(s)
        s.read_path(DECOY, "decoy")

    outcome = evaluate(build("negative", mixed))
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["discovery"]["lanes_used"] == ["bash", "dedicated"]
    assert outcome["evidence"]["discovery"]["search_calls"] == 3


# --- 規則 1-3 -----------------------------------------------------------------------------------------------


def test_rule1_no_stream_is_unavailable() -> None:
    assert_outcome(evaluate(""), "unavailable", 1)
    assert_outcome(evaluate(normal_stream(), claude_unavailable_reason="claude not found"), "unavailable", 1)


def test_rule2_permission_denial_of_discovery_or_required_read_is_unavailable_not_fail() -> None:
    s = Stream().init().agent_call().start().root().head().bundle()
    s.grep(_all_symbols_pattern(NEG_ROLES), "denied", tool_use_id="toolu_grep", is_error=True)
    s.stop().agent_result().final([{"tool_name": "Grep", "tool_use_id": "toolu_grep", "tool_input": {}}])
    assert_outcome(evaluate(s), "unavailable", 2)

    # Bash lane の eligible な find / grep が permission 拒否された場合も unavailable。
    for command in (
        f"grep -rl -E '{_all_symbols_pattern(NEG_ROLES)}' {ROOT}",
        f"find {ROOT} -type f -name '{EVAL_FRAG}*'",
    ):
        b = Stream().init().agent_call().start().root().head().bundle()
        b.bash(command, "denied", tool_use_id="toolu_bash", is_error=True)
        b.stop().agent_result().final([{"tool_name": "Bash", "tool_use_id": "toolu_bash", "tool_input": {}}])
        assert_outcome(evaluate(b), "unavailable", 2)

    # eligible 形状でない Bash の拒否は必須観測の拒否ではない（allowlist 外の試行として FAIL）。
    rg = Stream().init().agent_call().start().root().head().bundle()
    rg.bash(f"rg -n sym {ROOT}", "denied", tool_use_id="toolu_rg", is_error=True)
    rg.stop().agent_result().final([{"tool_name": "Bash", "tool_use_id": "toolu_rg", "tool_input": {}}])
    assert_outcome(evaluate(rg), "fail", 4, "bash_not_allowed:not_find_grep")

    def denied_read(s2: Stream) -> None:
        discovery_steps(s2, "negative")
        s2.read_role(NEG_ROLES, "producer", tool_use_id="toolu_read", is_error=True)

    s3 = Stream().init().agent_call().start()
    denied_read(s3)
    s3.stop().agent_result().final([{"tool_name": "Read", "tool_use_id": "toolu_read", "tool_input": {}}])
    assert_outcome(evaluate(s3), "unavailable", 2)


def test_rule2_absence_of_dedicated_grep_glob_alone_is_not_unavailable() -> None:
    """native build の既定 pool は専用 Grep / Glob を持たない。Bash lane が使えれば検証を続行する（PASS しうる）。"""
    bash_only = ["Read", "Bash", "Agent"]
    for kind in ("negative", "positive", "hybrid"):
        for lane in ("bash_grep", "bash_find"):
            s = Stream().init(bash_only).agent_call().start()
            discovery_steps(s, kind, lane)
            s.handback(json.dumps(default_result(kind)))
            assert_outcome(evaluate(s.stop().agent_result().final(), kind), "pass", 5)
    # 専用 Grep だけ / Glob だけがある pool も supported lane を持つ。
    for tools in (["Read", "Grep", "Agent"], ["Read", "Glob", "Agent"], [*bash_only, "Grep", "Glob"]):
        s = Stream().init(tools).agent_call().start()
        discovery_steps(s, "negative", "grep")
        s.handback(json.dumps(default_result("negative")))
        assert_outcome(evaluate(s.stop().agent_result().final()), "pass", 5)


def test_rule2_no_supported_lane_in_the_session_tool_pool_is_unavailable_and_never_pass() -> None:
    """Bash も専用 Grep / Glob も session に無ければ検証不能（unavailable であり PASS ではない）。"""

    def stream(tools: list[str] | None) -> Stream:
        s = Stream().init(tools).agent_call().start().root().head().bundle()
        s.grep(
            _all_symbols_pattern(NEG_ROLES),
            "<tool_use_error>No such tool available: Grep</tool_use_error>",
            is_error=True,
        )
        _read_all_targets(s)
        s.handback(json.dumps(default_result("negative")))
        return s.stop().agent_result().final()

    for tools in (["Read", "Agent"], [], ["Read", "Agent", "mcp__x__y"]):
        outcome = evaluate(stream(tools))
        assert_outcome(outcome, "unavailable", 2)
        assert "no supported discovery lane" in outcome["reason"]
        assert outcome["exit_code"] == 77
    # lane はあるのに discovery が成立しない場合は、通常どおり規則 4 の FAIL（unavailable にしない）。
    assert_outcome(evaluate(stream(["Read", "Agent", "Bash"])), "fail", 4, "no_successful_discovery")
    assert_outcome(evaluate(stream(["Read", "Agent", "Grep", "Glob"])), "fail", 4, "no_successful_discovery")
    # `init` に tools が無い stream は capability を判定しない（従来どおり規則 4 へ進む）。
    assert_outcome(evaluate(stream(None)), "fail", 4, "no_successful_discovery")
    # simple は discovery を要求しないため tool pool に依存しない。
    simple = (
        Stream().init(["Read", "Agent"]).agent_call().start().bundle().handback(json.dumps(CLEAR)).stop().agent_result()
    )
    assert_outcome(evaluate(simple.final(), "simple"), "pass", 5)


def test_session_tools_reader_helper_reads_only_the_structured_init_tools() -> None:
    init = [json.loads(line) for line in Stream().init(["Read", "Bash", "mcp__a__b"]).lines]
    assert NEW.session_tools(init) == ["Read", "Bash", "mcp__a__b"]
    assert NEW.session_tools([json.loads(line) for line in Stream().init().lines]) is None
    assert NEW.session_tools([]) is None
    assert NEW.supported_discovery_lanes(["Read"]) == []
    assert NEW.supported_discovery_lanes(["Read", "Bash"]) == ["bash"]
    assert NEW.supported_discovery_lanes(["Grep"]) == ["dedicated"]
    assert NEW.supported_discovery_lanes(["Glob", "Bash"]) == ["dedicated", "bash"]
    outcome = evaluate(normal_stream("negative", lane="bash_grep"))
    assert outcome["session_tools"] is None  # normal_stream は init に tools を持たない


def test_rule3_terminal_incomplete_is_fail_not_pass() -> None:
    complete = normal_stream("negative")
    assert NEW.terminal_incomplete_reason(complete.text(), timed_out=False) is None
    assert NEW.terminal_incomplete_reason(complete.text(), timed_out=True)
    reason = NEW.terminal_incomplete_reason(complete.text(), timed_out=True)
    assert_outcome(evaluate(complete, terminal_incomplete=reason), "fail", 3)
    max_turns = Stream().init().agent_call().start()
    discovery_steps(max_turns, "negative")
    max_turns.handback(json.dumps(default_result("negative"))).stop().agent_result().final(subtype="error_max_turns")
    assert NEW.terminal_incomplete_reason(max_turns.text(), timed_out=False)
    no_result = Stream().init().agent_call()
    assert NEW.terminal_incomplete_reason(no_result.text(), timed_out=False) == "no terminal result event"


def test_rule3_lifecycle_failures_are_fail() -> None:
    no_start = Stream().init().agent_call()
    discovery_steps(no_start, "negative")
    no_start.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    assert_outcome(evaluate(no_start), "fail", 3)
    two_agents = Stream().init().agent_call().start("agent-1").start("agent-2")
    discovery_steps(two_agents, "negative")
    two_agents.stop("agent-1").stop("agent-2").agent_result().final()
    assert_outcome(evaluate(two_agents), "fail", 3)


# --- parity: 規則 1-3 の outcome が #2963 の evaluator と一致する ---------------------------------------------------


def _parity_streams() -> list[tuple[str, Stream | str, dict[str, Any]]]:
    streams: list[tuple[str, Stream | str, dict[str, Any]]] = []
    streams.append(("normal", normal_stream("negative"), {}))
    streams.append(("no_stream", "", {}))
    streams.append(("unavailable_reason", normal_stream("negative"), {"unavailable": "claude not found"}))
    no_start = Stream().init().agent_call()
    discovery_steps(no_start, "negative")
    streams.append(("no_start", no_start.stop().agent_result().final(), {}))
    no_stop = Stream().init().agent_call().start()
    discovery_steps(no_stop, "negative")
    streams.append(("no_stop", no_stop.agent_result().final(), {}))
    stop_first = Stream().init().agent_call().stop()
    discovery_steps(stop_first, "negative")
    streams.append(("stop_before_start", stop_first.start().agent_result().final(), {}))
    different_ids = Stream().init().agent_call().start("agent-1")
    discovery_steps(different_ids, "negative")
    streams.append(("different_ids", different_ids.stop("agent-2").agent_result().final(), {}))
    two_ids = Stream().init().agent_call().start("agent-1").start("agent-2")
    discovery_steps(two_ids, "negative")
    streams.append(("two_ids", two_ids.stop("agent-1").stop("agent-2").agent_result().final(), {}))
    wrong_type = Stream().init().agent_call().start(agent_type="other-agent")
    discovery_steps(wrong_type, "negative")
    streams.append(("wrong_type", wrong_type.stop(agent_type="other-agent").agent_result().final(), {}))
    no_parent = Stream().init().start()
    discovery_steps(no_parent, "negative")
    streams.append(("no_parent_agent", no_parent.stop().final(), {}))
    two_parents = Stream().init().agent_call().agent_call("toolu_parent_2").start()
    discovery_steps(two_parents, "negative")
    streams.append(("two_parent_agents", two_parents.stop().agent_result().final(), {}))
    parent_error = Stream().init().agent_call().start()
    discovery_steps(parent_error, "negative")
    streams.append(("parent_result_error", parent_error.stop().agent_result(is_error=True).final(), {}))
    empty_interval = Stream().init().agent_call().start().stop().agent_result().final()
    streams.append(("empty_interval", empty_interval, {}))
    outside = Stream().init().agent_call()
    discovery_steps(outside, "negative")
    streams.append(("tools_outside_interval", outside.start().stop().agent_result().final(), {}))
    other_parent = Stream().init().agent_call().start()
    other_parent.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}", parent="toolu_other")
    streams.append(("only_other_parent_tools", other_parent.stop().agent_result().final(), {}))
    denied_root = Stream().init().agent_call().start()
    denied_root.root(tool_use_id="toolu_root", is_error=True, output="denied")
    streams.append(
        (
            "denied_root",
            denied_root.stop().agent_result().final([{"tool_name": "Bash", "tool_use_id": "toolu_root"}]),
            {},
        )
    )
    denied_parent = Stream().init().agent_call().start()
    discovery_steps(denied_parent, "negative")
    streams.append(
        (
            "denied_parent_agent",
            denied_parent.stop().agent_result().final([{"tool_name": "Agent", "tool_use_id": PARENT}]),
            {},
        )
    )
    denied_read = Stream().init().agent_call().start().root().head()
    denied_read.read_role(NEG_ROLES, "producer", tool_use_id="toolu_target_read", is_error=True)
    streams.append(
        (
            "denied_target_read",
            denied_read.stop().agent_result().final([{"tool_name": "Read", "tool_use_id": "toolu_target_read"}]),
            {},
        )
    )
    return streams


def _rules_1_3(outcome: dict[str, Any]) -> tuple[str, int] | str:
    return (outcome["verdict"], outcome["rule"]) if outcome["rule"] <= 3 else "continue"


def parity_mismatches() -> list[str]:
    """同一 stream に対する #2963 の evaluator と新 evaluator の規則 1-3 の outcome 不一致の一覧。

    比較は両 evaluator が共通に定義する規則 1-3（lifecycle / 親 Agent / reviewer 区間 / root・HEAD・target Read の
    permission 拒否）の範囲に限る。#2973 固有の discovery 観測（専用 lane と Bash lane）は #2963 側と比較しない。
    hybrid は #2963 contract では negative と同等に扱う（4 役の target path を全て必須とする）。"""
    paths = {role: NEG_ROLES[role]["path"] for role in NEG_ROLES}
    mismatches: list[str] = []
    for name, stream, options in _parity_streams():
        stdout = stream if isinstance(stream, str) else stream.text()
        old = OLD.evaluate_reachability_runtime(
            stdout=stdout,
            tested_head=HEAD,
            resolved_root=ROOT,
            invocation_dir=INV,
            fixture_kind="negative",
            fixture_paths=paths,
            raw_result=default_result("negative"),
            claude_unavailable_reason=options.get("unavailable"),
        )
        for kind, roles in (("negative", NEG_ROLES), ("hybrid", HYB_ROLES)):
            new = NEW.evaluate_bounded_discovery_runtime(
                stdout=stdout,
                tested_head=HEAD,
                resolved_root=ROOT,
                invocation_dir=INV,
                fixture_kind=kind,
                fixture_roles=roles,
                raw_result=default_result(kind),
                claude_unavailable_reason=options.get("unavailable"),
            )
            if _rules_1_3(old) != _rules_1_3(new):
                mismatches.append(f"{name}/{kind}: old={_rules_1_3(old)} new={_rules_1_3(new)}")
    return mismatches


def test_rules_1_3_outcomes_match_the_2963_evaluator_on_the_same_stream() -> None:
    streams = _parity_streams()
    assert len(streams) >= 15
    assert parity_mismatches() == []
    seen = set()
    for _name, stream, options in streams:
        stdout = stream if isinstance(stream, str) else stream.text()
        outcome = OLD.evaluate_reachability_runtime(
            stdout=stdout,
            tested_head=HEAD,
            resolved_root=ROOT,
            invocation_dir=INV,
            fixture_kind="negative",
            fixture_paths={role: NEG_ROLES[role]["path"] for role in NEG_ROLES},
            raw_result=default_result("negative"),
            claude_unavailable_reason=options.get("unavailable"),
        )
        seen.add(_rules_1_3(outcome))
    # 非 vacuous: unavailable / fail の別と、規則 3 を通過する経路の全てを parity が実際に区別している。
    assert {("unavailable", 1), ("unavailable", 2), ("fail", 3), "continue"} <= seen


def test_parity_test_can_fail_when_the_new_lifecycle_rules_diverge(monkeypatch: pytest.MonkeyPatch) -> None:
    """mutation: 新 evaluator の reviewer 判定を壊すと parity が不一致を検出する（false-green でない）。"""
    assert parity_mismatches() == []
    monkeypatch.setattr(NEW, "_reviewer_agent_tool_use_ids", lambda tool_uses: set())
    assert parity_mismatches() != []
    monkeypatch.undo()
    assert parity_mismatches() == []

    real = NEW._reviewer_invocation

    def lenient(lifecycle: list[dict[str, Any]]) -> dict[str, Any]:
        data = real(lifecycle)
        data["stop_ids"] = set(data["start_ids"])  # Start / Stop の agent_id 不一致を見逃す
        return data

    monkeypatch.setattr(NEW, "_reviewer_invocation", lenient)
    assert any(item.startswith("different_ids") for item in parity_mismatches())


# --- 規則 4: discovery / bound / scope / 因果 -----------------------------------------------------------------


def test_rule4_no_discovery_is_fail() -> None:
    def no_discovery(s: Stream) -> None:
        _prefix(s)
        _read_all_targets(s)

    outcome = evaluate(build("negative", no_discovery))
    assert_outcome(outcome, "fail", 4, "no_successful_discovery")
    assert any(v.startswith("read_before_discovery:consumer") for v in outcome["evidence"]["violations"])


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find"])
def test_rule4_discovery_outside_the_reviewer_interval_is_not_counted(lane: str) -> None:
    s = Stream().init().agent_call()
    s.root().head()  # Start より前（区間外）
    do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
    s.start().root().head().bundle()
    _read_all_targets(s)
    s.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 4, "no_successful_discovery")
    other_parent = Stream().init().agent_call().start().root().head().bundle()
    do_search(other_parent, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()], parent="toolu_x")
    _read_all_targets(other_parent)
    assert_outcome(evaluate(other_parent.stop().agent_result().final()), "fail", 4, "no_successful_discovery")


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find"])
def test_rule4_non_permission_search_failure_is_fail_not_unavailable(lane: str) -> None:
    def failed_search(s: Stream) -> None:
        _prefix(s)
        do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()], is_error=True)
        _read_all_targets(s)

    assert_outcome(evaluate(build("negative", failed_search)), "fail", 4, "no_successful_discovery")


@pytest.mark.parametrize("lane", ["grep", "bash_grep"])
def test_rule4_is_error_eligible_call_counts_toward_the_bound_but_is_not_attribution(lane: str) -> None:
    """grep exit 1（no match）/ exit 2 を含む is_error の eligible call は bound に算入され、帰属には使われない。"""

    def hits_in_error_result(s: Stream) -> None:
        _prefix(s)
        do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()], is_error=True)  # hit text 付きでも無効
        _read_all_targets(s)

    outcome = evaluate(build("negative", hits_in_error_result))
    assert_outcome(outcome, "fail", 4, "no_successful_discovery")
    assert outcome["evidence"]["discovery"]["roles"]["consumer"]["satisfied"] is False
    assert any(v.startswith("read_before_discovery:") for v in outcome["evidence"]["violations"])

    def errors_then_success(s: Stream, errors: int) -> None:
        _prefix(s)
        for _ in range(errors):
            do_search(s, lane, NEG_ROLES, [], is_error=True)  # no match（exit 1）
        do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
        _read_all_targets(s)

    ok = evaluate(build("negative", lambda s: errors_then_success(s, 7)))  # 7 error + 1 success = 8
    assert_outcome(ok, "pass", 5)
    assert ok["evidence"]["discovery"]["search_calls"] == 8
    assert_outcome(
        evaluate(build("negative", lambda s: errors_then_success(s, 8))), "fail", 4, "search_bound_exceeded:9>8"
    )


def _negative_with_extra(count: int, extra: Any) -> Stream:
    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(count):
            extra(s)

    return build("negative", mutate)


def _extra_grep(s: Stream) -> None:
    s.grep(EVAL_SYM, _hit_text(EVAL_PATH))


def _extra_bash_grep(s: Stream) -> None:
    s.bash_grep(EVAL_SYM, _abs_lines(EVAL_PATH))


def _extra_glob(s: Stream) -> None:
    s.glob(f"**/{EVAL_FRAG}*", _hit_text(EVAL_PATH))


def _extra_bash_find(s: Stream) -> None:
    s.bash_find(EVAL_FRAG, _abs_lines(EVAL_PATH))


@pytest.mark.parametrize("extra", [_extra_grep, _extra_bash_grep, _extra_glob, _extra_bash_find])
def test_rule4_search_call_bound_is_exactly_eight_in_each_lane(extra: Any) -> None:
    # normal は検索 1 回。追加 7 回で合計 8（上限ちょうど）は PASS、追加 8 回で 9 は FAIL。root / HEAD は数えない。
    passing = evaluate(_negative_with_extra(7, extra))
    assert_outcome(passing, "pass", 5)
    assert passing["evidence"]["discovery"]["search_calls"] == 8
    assert_outcome(evaluate(_negative_with_extra(8, extra)), "fail", 4, "search_bound_exceeded:9>8")


def test_rule4_search_call_bound_counts_mixed_lanes_together() -> None:
    def mixed(s: Stream) -> None:
        discovery_steps(s, "negative")  # grep 1
        for _ in range(2):
            _extra_glob(s)
        for _ in range(2):
            _extra_bash_grep(s)
        for _ in range(2):
            _extra_bash_find(s)
        s.grep(EVAL_SYM, "error", is_error=True)  # 失敗した検索も数える（合計 8）

    outcome = evaluate(build("negative", mixed))
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["discovery"]["search_calls"] == 8

    def mixed_over(s: Stream) -> None:
        mixed(s)
        _extra_bash_grep(s)  # 専用 4 + Bash 5 の合算で 9

    assert_outcome(evaluate(build("negative", mixed_over)), "fail", 4, "search_bound_exceeded:9>8")


def test_rule4_allowed_root_and_head_bash_are_not_search_calls() -> None:
    def repeated(s: Stream) -> None:
        _prefix(s)
        for _ in range(3):  # root / HEAD の再解決は non-discovery（bound に算入しない）
            s.root()
            s.head()
        do_search(s, "grep", NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
        for _ in range(7):
            _extra_bash_grep(s)
        _read_all_targets(s)

    outcome = evaluate(build("negative", repeated))
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["discovery"]["search_calls"] == 8
    rules = {r["rule"] for r in outcome["evidence"]["tool_use_classification"] if r["name"] == "Bash"}
    assert {"bash_exact_root_command", "bash_exact_head_command", "bash_find_grep_eligible_shape"} <= rules


def test_rule4_source_read_bound_is_exactly_eight_and_excludes_bundle_and_body() -> None:
    def reads(extra: int) -> Stream:
        def mutate(s: Stream) -> None:
            discovery_steps(s, "negative")  # target 4 + decoy 1 = 5 Read（bundle / body は数えない）
            for n in range(extra):
                s.read_path(f"{SYN}/other_{n}.py")

        return build("negative", mutate)

    assert_outcome(evaluate(reads(3)), "pass", 5)  # 合計 8
    assert_outcome(evaluate(reads(4)), "fail", 4, "read_bound_exceeded:9>8")

    def many_bundle_reads(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(12):
            s.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")
            s.tool("Read", {"file_path": f"{INV}/body.md"}, "body")

    assert_outcome(evaluate(build("negative", many_bundle_reads)), "pass", 5)

    def lookalike(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(4):
            s.tool("Read", {"file_path": f"{INV}/sub/bundle.json"}, "{}")  # 直下ではない file は対象

    assert_outcome(evaluate(build("negative", lookalike)), "fail", 4, "read_bound_exceeded")


def _scoped_search(lane: str, path: str | None) -> Any:
    """`path` を差し替えた検索を 1 件足す mutate（Bash の `path=None` は path operand の省略）。"""

    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        if lane == "grep":
            s.grep(EVAL_SYM, _hit_text(EVAL_PATH), path=path)
        elif lane == "glob":
            s.glob(f"**/{EVAL_FRAG}*", _hit_text(EVAL_PATH), path=path)
        elif lane == "bash_grep":
            s.bash(f"grep -rl {EVAL_SYM}" + (f" {path}" if path is not None else ""), _abs_lines(EVAL_PATH))
        else:
            s.bash(f"find {path or ''} -type f -name '{EVAL_FRAG}*'".replace("find  ", "find "), _abs_lines(EVAL_PATH))

    return mutate


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/synthetic/elsewhere", "search_outside_root"),
        (f"{ROOT}/../outside", "search_outside_root"),
        (f"{ROOT}-sibling", "search_outside_root"),
        (None, "search_path_omitted"),
        ("relative/dir", "search_path_not_absolute"),
    ],
)
def test_rule4_search_scope_violations_are_fail_in_every_lane(lane: str, path: str | None, expected: str) -> None:
    label = {"grep": "Grep", "glob": "Glob", "bash_grep": "Bash:grep", "bash_find": "Bash:find"}[lane]
    assert_outcome(evaluate(build("negative", _scoped_search(lane, path))), "fail", 4, f"{expected}:{label}")


def test_rule4_dedicated_empty_path_is_omitted_and_inside_root_directory_is_allowed() -> None:
    assert_outcome(evaluate(build("negative", _scoped_search("grep", ""))), "fail", 4, "search_path_omitted:Grep")
    inside = f"{ROOT}/{SYN}"
    for lane in LANES:
        assert_outcome(evaluate(build("negative", _scoped_search(lane, inside))), "pass", 5)


def test_rule4_glob_pattern_escaping_or_pointing_outside_root_is_fail() -> None:
    for pattern in (f"/synthetic/elsewhere/**/{EVAL_FRAG}*", f"../../{EVAL_FRAG}*"):

        def mutate(s: Stream, pattern: str = pattern) -> None:
            discovery_steps(s, "negative")
            s.glob(pattern, _hit_text(EVAL_PATH))

        assert_outcome(evaluate(build("negative", mutate)), "fail", 4, "search_outside_root:Glob")

    # find の絶対 `-path` pattern が root 外を指す場合も違反。
    def find_outside(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(f"find {ROOT} -type f -path '/synthetic/elsewhere/*{EVAL_FRAG}*'", "")

    assert_outcome(evaluate(build("negative", find_outside)), "fail", 4, "search_outside_root:Bash:find")

    def find_inside(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(f"find {ROOT} -type f -path '{ROOT}/{SYN}/*{EVAL_FRAG}*'", _abs_lines(EVAL_PATH))

    assert_outcome(evaluate(build("negative", find_inside)), "pass", 5)


def _unrelated(mutate_search: Any) -> Stream:
    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        mutate_search(s)

    return build("negative", mutate)


def test_rule4_unrelated_queries_are_fail_with_each_tool_checked_separately() -> None:
    cases = {
        "irrelevant_query:Grep": lambda s: s.grep("totally_unrelated_symbol", _hit_text(EVAL_PATH)),
        "irrelevant_query:Glob": lambda s: s.glob("**/*.py", _hit_text(EVAL_PATH)),
        "irrelevant_query:Bash:grep": lambda s: s.bash_grep("totally_unrelated_symbol", _abs_lines(EVAL_PATH)),
        "irrelevant_query:Bash:find": lambda s: s.bash(f"find {ROOT} -type f -name '*.py'", _abs_lines(EVAL_PATH)),
    }
    for violation, search in cases.items():
        assert_outcome(evaluate(_unrelated(search)), "fail", 4, violation)
    # Grep / grep は symbol、Glob / find は file 名断片が関連性（取り違えは違反）。
    swapped = {
        "irrelevant_query:Grep": lambda s: s.grep(EVAL_FRAG, _hit_text(EVAL_PATH)),
        "irrelevant_query:Glob": lambda s: s.glob(f"**/{EVAL_SYM}*", _hit_text(EVAL_PATH)),
        "irrelevant_query:Bash:grep": lambda s: s.bash_grep(EVAL_FRAG, _abs_lines(EVAL_PATH)),
        "irrelevant_query:Bash:find": lambda s: s.bash_find(EVAL_SYM, _abs_lines(EVAL_PATH)),
    }
    for violation, search in swapped.items():
        assert_outcome(evaluate(_unrelated(search)), "fail", 4, violation)


@pytest.mark.parametrize("lane", LANES)
def test_rule4_result_without_the_target_path_does_not_justify_the_target_read(lane: str) -> None:
    def no_target_in_result(s: Stream) -> None:
        _prefix(s)
        do_search(s, lane, NEG_ROLES, [DECOY, f"{SYN}/test_self.py"])  # target path が無い
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role, output="no import hints")

    outcome = evaluate(build("negative", no_target_in_result))
    assert_outcome(outcome, "fail", 4, "read_before_discovery")
    assert "no_successful_discovery" in outcome["evidence"]["violations"]


@pytest.mark.parametrize("lane", ["grep", "bash_grep"])
def test_rule4_content_line_that_merely_contains_the_target_path_is_not_attribution(lane: str) -> None:
    """(a) 構造的な帰属: P_R は hit した file の path としてだけ数える。matched content に P_R を含む行は数えない。"""
    metadata = "\n".join(
        f'{SYN}/meta.json:{n}:  "path": "{role["path"]}" ## {role["symbol"]}'
        for n, role in enumerate(NEG_ROLES.values(), 1)
    )

    def content_only(s: Stream) -> None:
        _prefix(s)
        pattern = _all_symbols_pattern(NEG_ROLES)
        if lane == "grep":
            s.grep(pattern, metadata)
        else:
            s.bash_grep(pattern, metadata, flags="-rn")
        _read_all_targets(s)

    assert_outcome(evaluate(build("negative", content_only)), "fail", 4, "read_before_discovery")

    def hit_file_prefix(s: Stream) -> None:  # `<file>:<line>:<content>` の file 部分が P_R なら帰属する
        _prefix(s)
        lines = "\n".join(f"{ROOT}/{role['path']}:2:def {role['symbol']}():" for role in NEG_ROLES.values())
        pattern = _all_symbols_pattern(NEG_ROLES)
        if lane == "grep":
            s.grep(pattern, lines)
        else:
            s.bash_grep(pattern, lines, flags="-rn")
        _read_all_targets(s)

    assert_outcome(evaluate(build("negative", hit_file_prefix)), "pass", 5)


def test_rule4_path_lines_of_glob_and_find_must_be_exact_paths_not_content_lines() -> None:
    """Glob / find の結果は path 行そのものでなければ帰属しない（`<path>:<n>:` の content 形式は grep だけ）。"""
    content_form = "\n".join(f"{ROOT}/{r['path']}:3:x" for r in NEG_ROLES.values())

    def glob_content(s: Stream) -> None:
        _prefix(s)
        for role in NEG_ROLES.values():
            s.glob(f"**/{role['file_fragment']}*", content_form)
        _read_all_targets(s)

    def find_content(s: Stream) -> None:
        _prefix(s)
        names = " -o -name ".join(f"'{r['file_fragment']}*'" for r in NEG_ROLES.values())
        s.bash(f"find {ROOT} -type f -name {names}", content_form)
        _read_all_targets(s)

    for mutate in (glob_content, find_content):
        assert_outcome(evaluate(build("negative", mutate)), "fail", 4, "read_before_discovery")


def test_rule4_self_referential_literal_hit_is_not_a_discovery_success() -> None:
    """body・test・evaluator 内に target path が literal として現れるだけの hit（他 file の本文行）は数えない。"""
    literal = "\n".join(f"{SYN}/test_self.py:{n}:  '{r['path']}'" for n, r in enumerate(NEG_ROLES.values(), 1))

    def self_ref(s: Stream) -> None:
        _prefix(s)
        s.grep(_all_symbols_pattern(NEG_ROLES), literal)
        _read_all_targets_no_hint(s)

    assert_outcome(evaluate(build("negative", self_ref)), "fail", 4, "read_before_discovery")
    producer = NEG_ROLES["producer"]["path"]
    assert not NEW.result_lists_path(literal, ROOT, producer)
    assert NEW.result_lists_path(_hit_text(producer), ROOT, producer)
    assert NEW.result_lists_path(f"{ROOT}/{producer}:3:x", ROOT, producer)
    assert NEW.result_lists_path(f"./{producer}", ROOT, producer)
    assert not NEW.result_lists_path(f"{producer}.bak", ROOT, producer)
    # `path` mode（Glob / find）は path 単独の行だけ。`grep` mode は `<path>:` で始まる行も数える。
    assert NEW.result_lists_path(f"{producer}:3:x", ROOT, producer, "grep")
    assert not NEW.result_lists_path(f"{producer}:3:x", ROOT, producer, "path")
    assert NEW.result_lists_path(f"{ROOT}/{producer}\n", ROOT, producer, "path")
    assert not NEW.result_lists_path(f"{producer} matched here", ROOT, producer, "grep")


def _read_all_targets_no_hint(s: Stream) -> None:
    for role in ("consumer", "evaluator", "producer", "parser"):
        s.read_role(NEG_ROLES, role, output="no import hints")


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find", "glob"])
def test_rule4_read_before_discovery_is_fail_even_if_a_dummy_search_follows(lane: str) -> None:
    def guess_then_search(s: Stream) -> None:
        _prefix(s)
        s.read_role(NEG_ROLES, "consumer")  # 推測 Read（discovery より前）
        do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
        for role in ("evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", guess_then_search)), "fail", 4, "read_before_discovery:consumer")


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find"])
def test_rule4_decoy_only_read_without_the_target_is_fail(lane: str) -> None:
    def decoy_only(s: Stream) -> None:
        _prefix(s)
        do_search(s, lane, NEG_ROLES, [*(r["path"] for r in NEG_ROLES.values()), DECOY])
        for role in ("consumer", "producer", "parser"):
            s.read_role(NEG_ROLES, role)
        s.read_path(DECOY, "decoy source")  # evaluator の target を Read しない

    outcome = evaluate(build("negative", decoy_only))
    assert_outcome(outcome, "fail", 4, "missing_target_read:evaluator")
    assert outcome["evidence"]["violations"] == ["missing_target_read:evaluator"]


def test_rule4_hybrid_re_search_of_an_explicit_path_role_is_fail_in_every_tool() -> None:
    producer, parser = HYB_ROLES["producer"], HYB_ROLES["parser"]
    cases = {
        "irrelevant_query:Grep": lambda s: s.grep(producer["symbol"], _hit_text(producer["path"])),
        "irrelevant_query:Glob": lambda s: s.glob(f"**/{parser['file_fragment']}*", _hit_text(parser["path"])),
        "irrelevant_query:Bash:grep": lambda s: s.bash_grep(producer["symbol"], _abs_lines(producer["path"])),
        "irrelevant_query:Bash:find": lambda s: s.bash_find(parser["file_fragment"], _abs_lines(parser["path"])),
    }
    for violation, search in cases.items():

        def re_search(s: Stream, search: Any = search) -> None:
            discovery_steps(s, "hybrid")
            search(s)

        assert_outcome(evaluate(build("hybrid", re_search), "hybrid"), "fail", 4, violation)


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find", "glob"])
def test_rule4_hybrid_unresolved_role_without_discovery_is_fail(lane: str) -> None:
    def no_discovery(s: Stream) -> None:
        _prefix(s)
        for role in ("producer", "parser", "consumer", "evaluator"):
            s.read_role(HYB_ROLES, role)

    assert_outcome(evaluate(build("hybrid", no_discovery), "hybrid"), "fail", 4, "no_successful_discovery")


@pytest.mark.parametrize("lane", ["grep", "bash_grep"])
def test_rule4_target_derived_from_the_consumer_import_is_not_a_violation(lane: str) -> None:
    """帰属規則 (b): 既に Read した consumer の結果 text に module 名が現れる target の Read は discovery 不要。"""

    def search_consumer(s: Stream) -> None:
        hits = [NEG_ROLES["consumer"]["path"], DECOY]
        if lane == "grep":
            s.grep(NEG_ROLES["consumer"]["symbol"], _hit_text(*hits))
        else:
            s.bash_grep(NEG_ROLES["consumer"]["symbol"], _abs_lines(*hits))

    def via_import(s: Stream) -> None:
        _prefix(s)
        search_consumer(s)
        s.read_role(NEG_ROLES, "consumer")  # import: zz_evaluator / zz_producer
        s.read_role(NEG_ROLES, "evaluator")
        s.read_role(NEG_ROLES, "producer")  # producer の text は zz_parser を含む
        s.read_role(NEG_ROLES, "parser")

    outcome = evaluate(build("negative", via_import))
    assert_outcome(outcome, "pass", 5)
    roles = outcome["evidence"]["discovery"]["roles"]
    assert roles["consumer"]["via"] == "discovery"
    assert roles["evaluator"]["via"] == "import" and roles["producer"]["via"] == "import"
    assert roles["parser"]["via"] == "import"

    def import_without_hint(s: Stream) -> None:  # consumer の text に evaluator の module 名が無ければ導出できない
        _prefix(s)
        search_consumer(s)
        s.read_role(NEG_ROLES, "consumer", output="1\tno imports here")
        for role in ("evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", import_without_hint)), "fail", 4, "read_before_discovery:evaluator")


def test_rule4_a_decoy_module_name_does_not_justify_the_target() -> None:
    def decoy_import_only(s: Stream) -> None:
        _prefix(s)
        s.grep(NEG_ROLES["consumer"]["symbol"], _hit_text(NEG_ROLES["consumer"]["path"]))
        s.read_role(NEG_ROLES, "consumer", output="1\tfrom zz_stale_evaluator import x\n")
        for role in ("evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", decoy_import_only)), "fail", 4, "read_before_discovery:evaluator")


@pytest.mark.parametrize("lane", LANES)
def test_rule4_one_search_may_serve_several_roles(lane: str) -> None:
    outcome = evaluate(normal_stream("negative", lane=lane))
    if lane != "glob":  # Glob は role ごとに 1 件（file 名断片が role 単位のため）。それ以外は 1 件で 4 役を兼ねる
        assert outcome["evidence"]["discovery"]["search_calls"] == 1
    assert all(r["via"] in ("discovery", "import") for r in outcome["evidence"]["discovery"]["roles"].values())


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find"])
def test_rule4_root_and_head_must_precede_discovery_and_match(lane: str) -> None:
    def head_after_search(s: Stream) -> None:
        s.root()
        do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
        s.head().bundle()
        _read_all_targets(s)

    assert_outcome(evaluate(build("negative", head_after_search)), "fail", 4, "before_head")

    def wrong_head(s: Stream) -> None:
        s.root().head(output=OTHER_HEAD + "\n").bundle()
        _read_all_targets(s)

    assert_outcome(evaluate(build("negative", wrong_head)), "fail", 4, "head_resolution_missing")

    def no_root(s: Stream) -> None:
        s.head().bundle()
        do_search(s, lane, NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
        _read_all_targets(s)

    assert_outcome(evaluate(build("negative", no_root)), "fail", 4, "root_resolution_missing")


# --- Bash allowlist / eligible 形状 ----------------------------------------------------------------------------

_BYPASS_COMMANDS = {
    "rg": f"rg -n {EVAL_SYM} {ROOT}",
    "git_grep": f"git grep {EVAL_SYM}",
    "git_ls_files": "git ls-files",
    "ls_R": f"ls -R {ROOT}",
    "cd_root_grep": f"cd {ROOT} && grep -rl {EVAL_SYM} .",
    "timeout_grep": f"timeout 5 grep -rl {EVAL_SYM} {ROOT}",
    "xargs_grep": f"xargs grep {EVAL_SYM}",
    "env_grep": f"env grep -rl {EVAL_SYM} {ROOT}",
    "cat": f"cat {ROOT}/{EVAL_PATH}",
    "sed": f"sed -n 1,20p {ROOT}/{EVAL_PATH}",
    "ugrep": f"ugrep -rl {EVAL_SYM} {ROOT}",
}


@pytest.mark.parametrize("name", sorted(_BYPASS_COMMANDS))
def test_rule4_allowlist_bypass_bash_is_a_violation_and_never_passes(name: str) -> None:
    command = _BYPASS_COMMANDS[name]

    def alone(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(command, _abs_lines(EVAL_PATH))

    outcome = evaluate(build("negative", alone))
    assert_outcome(outcome, "fail", 4, "bash_not_allowed")
    assert outcome["evidence"]["discovery"]["search_calls"] == 2  # eligible 1 + 非 allowlist 1（検索を隠さない）

    def with_seven_eligible(s: Stream) -> None:  # eligible 7 + bypass 1 = 8 でも bound 内の PASS にならない
        discovery_steps(s, "negative")
        for _ in range(6):
            _extra_bash_grep(s)
        s.bash(command, _abs_lines(EVAL_PATH))

    assert_outcome(evaluate(build("negative", with_seven_eligible)), "fail", 4, "bash_not_allowed")

    def with_eight_eligible(s: Stream) -> None:  # eligible 最大 8 回 + bypass 1 回 = 9
        discovery_steps(s, "negative")
        for _ in range(7):
            _extra_bash_grep(s)
        s.bash(command, _abs_lines(EVAL_PATH))

    over = evaluate(build("negative", with_eight_eligible))
    assert_outcome(over, "fail", 4, "bash_not_allowed")
    assert "search_bound_exceeded:9>8" in over["evidence"]["violations"]
    # bypass の Bash は discovery と認定されない（lane の根拠にならない）。
    bypass = [r for r in over["evidence"]["tool_use_classification"] if r["rule"].startswith("bash_not_allowlisted")]
    assert len(bypass) == 1 and bypass[0]["classification"] == "non_discovery" and bypass[0]["counted_as_search"]


def test_rule4_bypass_bash_cannot_replace_the_required_discovery() -> None:
    """eligible discovery を行わず、非 allowlist の Bash で source を取得しても PASS にならない。"""

    def cat_instead(s: Stream) -> None:
        _prefix(s)
        s.bash(f"rg -n {EVAL_SYM} {ROOT}", _abs_lines(EVAL_PATH))
        s.bash(f"cat {ROOT}/{EVAL_PATH}", "source")

    outcome = evaluate(build("negative", cat_instead))
    assert_outcome(outcome, "fail", 4, "no_successful_discovery")
    assert "missing_target_read:evaluator" in outcome["evidence"]["violations"]
    assert "bash_not_allowed:not_find_grep" in outcome["evidence"]["violations"]


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (f"grep -rl {EVAL_SYM} {ROOT};ls", "compound_operator"),  # 空白なしの `;`
        (f"grep -rl {EVAL_SYM} {ROOT} ;ls", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT}&&ls", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT}||ls", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT}|head", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT}>/tmp/x", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT} 2>&1", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT}&", "compound_operator"),
        (f"grep -rl {EVAL_SYM} {ROOT}\nls", "newline"),
        (f"grep -rl $(ls) {ROOT}", "command_substitution"),
        (f'grep -rl "$(ls)" {ROOT}', "command_substitution"),
        (f"grep -rl `ls` {ROOT}", "command_substitution"),
        (f"grep -rl {EVAL_SYM} '{ROOT}", "tokenize_error"),
        (f"grep --include '*.py' {EVAL_SYM} {ROOT}", "unsupported_flag:--include"),
        (f"grep -rlz {EVAL_SYM} {ROOT}", "unsupported_flag:-rlz"),
        (f"grep -rlP {EVAL_SYM} {ROOT}", "unsupported_flag:-rlP"),
        (f"grep -ePAT {ROOT}", "unsupported_flag:-ePAT"),
        (f"grep --exclude-dir {EVAL_SYM} {ROOT}", "unsupported_flag:--exclude-dir"),
        (f"grep --include= {EVAL_SYM} {ROOT}", "unsupported_flag:--include="),
        (f"grep -rl {EVAL_SYM} {ROOT} --color=always", "unsupported_flag:--color=always"),
        (f"find {ROOT} -name x -exec cat '{{}}' +", "unsupported_primary:-exec"),
        (f"find {ROOT} -name x -delete", "unsupported_primary:-delete"),
        (f"find {ROOT} -name x -execdir ls '{{}}' ';'", "compound_operator"),
        (f"find {ROOT} -name x -fprint /tmp/x", "unsupported_primary:-fprint"),
        (f"find {ROOT} -not -name x", "unsupported_primary:-not"),
        (f"find {ROOT} ! -name x", "unsupported_primary:!"),
        (f"find {ROOT} -name x -prune", "unsupported_primary:-prune"),
        (f"find {ROOT} -name x -print", "unsupported_primary:-print"),
        (f"find {ROOT} \\( -name x \\)", "compound_operator"),
        (f"find {ROOT} -maxdepth x -name y", "unsupported_primary:-maxdepth"),
    ],
)
def test_bash_shape_ineligible_commands_fail_closed(command: str, reason: str) -> None:
    parsed = NEW.parse_bash_search(command, ROOT)
    assert parsed["reason"] == reason, parsed

    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(command, _abs_lines(EVAL_PATH))

    assert_outcome(evaluate(build("negative", mutate)), "fail", 4, "bash_not_allowed")


@pytest.mark.parametrize(
    "command",
    [
        f"grep -rn -E '{EVAL_SYM}|fn_other_zz' {ROOT}/{SYN}",  # quote された `|` は引数の一部
        f"grep -rln -E '{EVAL_SYM}|fn_other_zz' {ROOT} --include='*.py' --exclude-dir=node_modules",
        f"grep -rn {EVAL_SYM} {ROOT}",  # `-rn` 連結 flag
        f"grep -rnwi {EVAL_SYM} {ROOT}",
        f"grep -rHI -F {EVAL_SYM} {ROOT}",
        f"grep -R -n -e {EVAL_SYM} {ROOT}",
        f"grep -rne {EVAL_SYM} {ROOT}",
        f"grep -rl {EVAL_SYM} {ROOT}/{SYN} {ROOT}/other",
        f"find {ROOT} -type f -name '{EVAL_FRAG}*.py'",
        f"find {ROOT} -maxdepth 6 -type f -iname '*{EVAL_FRAG}*' -o -path '*/{EVAL_FRAG}/*'",
        f"find {ROOT}/{SYN} {ROOT}/other -name '{EVAL_FRAG}*'",
    ],
)
def test_bash_shape_eligible_commands_are_discovery(command: str) -> None:
    parsed = NEW.parse_bash_search(command, ROOT)
    assert parsed["reason"] is None and parsed["scope_violation"] is None, parsed

    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(command, _abs_lines(EVAL_PATH))

    outcome = evaluate(build("negative", mutate))
    assert_outcome(outcome, "pass", 5)
    extra = [r for r in outcome["evidence"]["tool_use_classification"] if r["rule"] == "bash_find_grep_eligible_shape"]
    assert len(extra) == 1 and extra[0]["classification"] == "bash_lane_discovery" and extra[0]["lane"] == "bash"


def test_bash_shape_a_semicolon_without_spaces_is_not_eligible_discovery() -> None:
    """`a;b`（空白なし）は shlex の punctuation token として独立し、eligible 扱いにならない（fail closed）。"""
    assert NEW.parse_bash_search("a;b", ROOT)["reason"] == "compound_operator"
    assert NEW.parse_bash_search(f"grep -rl x {ROOT};cat y", ROOT)["reason"] == "compound_operator"
    assert NEW.parse_bash_search(f"grep -rl x {ROOT}&&cat y", ROOT)["reason"] == "compound_operator"

    def attributed_only_by_compound(s: Stream) -> None:
        _prefix(s)
        s.bash(
            f"grep -rl -E '{_all_symbols_pattern(NEG_ROLES)}' {ROOT};true",
            _abs_lines(*(r["path"] for r in NEG_ROLES.values())),
        )
        _read_all_targets(s)

    outcome = evaluate(build("negative", attributed_only_by_compound))
    assert_outcome(outcome, "fail", 4, "bash_not_allowed:compound_operator")
    assert "no_successful_discovery" in outcome["evidence"]["violations"]


def test_bash_shape_a_quoted_pipe_and_a_clustered_flag_are_eligible() -> None:
    quoted = NEW.parse_bash_search(f"grep -rl -E 'SymA|SymB' {ROOT}", ROOT)
    assert quoted["reason"] is None and quoted["texts"] == ["SymA|SymB"]
    cluster = NEW.parse_bash_search(f"grep -rn SymA {ROOT}", ROOT)
    assert cluster["reason"] is None and cluster["texts"] == ["SymA"]
    # 連結 flag は全文字が許可集合に含まれる場合のみ。
    assert NEW.parse_bash_search(f"grep -rnX SymA {ROOT}", ROOT)["reason"] == "unsupported_flag:-rnX"
    # `-e` の pattern は positional ではなく `-e` の値。残りの positional は path operand。
    with_e = NEW.parse_bash_search(f"grep -e SymA -e SymB {ROOT}/a {ROOT}/b", ROOT)
    assert with_e["texts"] == ["SymA", "SymB"] and with_e["paths"] == [f"{ROOT}/a", f"{ROOT}/b"]


def test_bash_shape_path_operand_rules() -> None:
    cases = {
        f"grep -rl SymA {ROOT}": None,
        "grep -rl SymA": "search_path_omitted",
        "grep -rl SymA relative/dir": "search_path_not_absolute",
        "grep -rl SymA .": "search_path_not_absolute",
        f"grep -rl SymA {ROOT}/../outside": "search_outside_root",
        f"grep -rl SymA {ROOT}/a/../b": "search_outside_root",
        "grep -rl SymA /synthetic/elsewhere": "search_outside_root",
        f"grep -rl SymA {ROOT}-sibling": "search_outside_root",
        f"grep -rl SymA {ROOT} /synthetic/elsewhere": "search_outside_root",
        f"find {ROOT}/../x -name f": "search_outside_root",
        "find -name f": "search_path_omitted",
        "find . -name f": "search_path_not_absolute",
    }
    for command, expected in cases.items():
        parsed = NEW.parse_bash_search(command, ROOT)
        assert parsed["reason"] is None, (command, parsed)
        assert parsed["scope_violation"] == expected, (command, parsed)


_EXPANSION_PATH_OPERANDS = (
    # OWNER review の false-green: 字句上は root 配下だが、expansion 後は `<root>/../../etc` になる。
    f"{ROOT}/${{HOME:0:0}}../${{HOME:0:0}}../etc",
    f"{ROOT}/$HOME",  # parameter expansion
    f"{ROOT}/${{UNSET_VAR:-..}}/..",  # `${...}` の既定値で `..` を構築
    f"{ROOT}/{{a,..}}/etc",  # brace expansion
    f"{ROOT}/*",  # pathname globbing
    f"{ROOT}/?/etc",
    f"{ROOT}/[a-z]*",
    f"~/{SYN}",  # tilde expansion
    f"{ROOT}/~",
)


def _search_commands(operand: str) -> tuple[str, str]:
    return f"grep -rl {EVAL_SYM} {operand}", f"find {operand} -type f -name '{EVAL_FRAG}*'"


@pytest.mark.parametrize("operand", _EXPANSION_PATH_OPERANDS)
def test_bash_unquoted_path_operand_with_shell_expansion_is_not_eligible(operand: str) -> None:
    """quote の外の path operand の shell expansion は parse 段階で fail closed（reason が立つ）。"""
    # 語中の `~` は shell では展開されないが、path operand の token 検査が fail closed にする。
    mid_word_tilde = operand.endswith("/~")
    for command in (*_search_commands(operand), f"grep -rl {EVAL_SYM} {ROOT} {operand}"):
        parsed = NEW.parse_bash_search(command, ROOT)
        if mid_word_tilde:
            assert parsed["reason"] is None and parsed["scope_violation"] == "search_path_shell_expansion", parsed
        else:
            assert parsed["reason"] == "shell_expansion", (command, parsed)


@pytest.mark.parametrize("operand", _EXPANSION_PATH_OPERANDS + ('"$HOME"',))
def test_bash_double_quoted_path_operand_with_shell_expansion_is_a_scope_violation(operand: str) -> None:
    """double quote 内でも `$` は展開される。quote 済みの path operand の expansion 文字も scope 違反。"""
    quoted = f'"{operand}"' if not operand.startswith('"') else operand
    for command in _search_commands(quoted):
        parsed = NEW.parse_bash_search(command, ROOT)
        assert parsed["reason"] is None, (command, parsed)
        assert parsed["scope_violation"] == "search_path_shell_expansion", (command, parsed)
    mixed = NEW.parse_bash_search(f"grep -rl {EVAL_SYM} {ROOT} {quoted}", ROOT)
    assert mixed["scope_violation"] == "search_path_shell_expansion", mixed


@pytest.mark.parametrize("lane", ["bash_grep", "bash_find"])
@pytest.mark.parametrize("quoted", [False, True])
def test_rule4_bash_path_operand_expansion_false_green_is_fail_even_with_valid_discovery(
    lane: str, quoted: bool
) -> None:
    """root 外へ展開されうる検索を 1 回混ぜると、正常な discovery が後続しても Rule 4 FAIL（false-green 禁止）。"""
    operand = f"{ROOT}/${{HOME:0:0}}../${{HOME:0:0}}../etc"
    operand = f'"{operand}"' if quoted else operand

    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(_search_commands(operand)[0 if lane == "bash_grep" else 1], _abs_lines(EVAL_PATH))

    label = "Bash:grep" if lane == "bash_grep" else "Bash:find"
    expected = f"search_path_shell_expansion:{label}" if quoted else "bash_not_allowed:shell_expansion"
    assert_outcome(evaluate(build("negative", mutate)), "fail", 4, expected)


OUTSIDE = "/synthetic/repo-root-sibling"

# 2nd review の repro: unquoted の pattern operand が brace expansion / `$IFS` word splitting / glob で、
# 実行時 argv に root 外の search path を足す（pattern には symbol が含まれるため relevance は通ってしまう）。
_UNQUOTED_PATTERN_INJECTIONS = {
    f"grep -rl {{{EVAL_SYM},{OUTSIDE}}} {ROOT}/src": "shell_expansion",
    f"grep -rl -e {{{EVAL_SYM},{OUTSIDE}}} {ROOT}/src": "shell_expansion",
    f"grep -rl {EVAL_SYM}$IFS{OUTSIDE} {ROOT}/src": "shell_expansion",
    f"grep -rl {EVAL_SYM}${{IFS}}{OUTSIDE} {ROOT}/src": "shell_expansion",
    f"grep -rl {EVAL_SYM}* {ROOT}/src": "shell_expansion",  # cwd 相対 operand を足しうる glob
    f"grep -rl {EVAL_SYM}? {ROOT}/src": "shell_expansion",
    f"grep -rl {EVAL_SYM}[a-z] {ROOT}/src": "shell_expansion",
    f"grep -rl ~ {ROOT}/src": "shell_expansion",
    f"find {ROOT}/src -name {EVAL_FRAG}* -type f": "shell_expansion",  # unquoted の -name glob
    f"find {ROOT}/src -type f -name {{{EVAL_FRAG},x}}": "shell_expansion",
    f"grep -rl --include=*.py {EVAL_SYM} {ROOT}/src": "shell_expansion",  # unquoted の flag glob
    # 行継続: `.\<改行>.` は shell が `..` へ連結し `<root>/src/../outside` を検索する。
    f"grep -rl {EVAL_SYM} {ROOT}/src/.\\\n./outside": "line_continuation",
    f'grep -rl {EVAL_SYM} "{ROOT}/src/.\\\n./outside"': "line_continuation",  # double quote 内でも連結される
    f"find {ROOT}/src/.\\\n./outside -name '{EVAL_FRAG}*'": "line_continuation",
}


@pytest.mark.parametrize(("command", "reason"), list(_UNQUOTED_PATTERN_INJECTIONS.items()))
def test_bash_unquoted_expansion_in_any_token_or_line_continuation_is_not_eligible(command: str, reason: str) -> None:
    parsed = NEW.parse_bash_search(command, ROOT)
    assert parsed["reason"] == reason, (command, parsed)

    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(command, _abs_lines(EVAL_PATH))

    outcome = evaluate(build("negative", mutate))  # 正常な discovery が後続・先行していても FAIL
    assert_outcome(outcome, "fail", 4, f"bash_not_allowed:{reason}")


def test_bash_backslash_carriage_return_is_also_a_line_continuation_hazard() -> None:
    command = f"grep -rl {EVAL_SYM} {ROOT}/src/.\\\r./outside"
    assert NEW.parse_bash_search(command, ROOT)["reason"] == "line_continuation"


@pytest.mark.parametrize(
    "command",
    [
        f"grep -rl {EVAL_SYM} {ROOT}/\\.\\./outside",  # `\.\.` は shlex で `..` になり path 判定で fail closed
        f"grep -rl {EVAL_SYM} {ROOT}/src/\\$HOME",  # escape した `$` も token に `$` が残り fail closed
        f"grep -rl {EVAL_SYM} {ROOT}/src/\\*",
    ],
)
def test_bash_backslash_escapes_in_path_operands_stay_fail_closed(command: str) -> None:
    parsed = NEW.parse_bash_search(command, ROOT)
    assert parsed["reason"] is not None or parsed["scope_violation"] is not None, (command, parsed)


@pytest.mark.parametrize(
    "command",
    [
        f"grep -rl 'SymA|{EVAL_SYM}' {ROOT}/src",  # quote した alternation
        f"grep -rl -e '{EVAL_SYM}' {ROOT}/src",  # `-e 'SymA'`
        f'grep -rl "{EVAL_SYM}$" {ROOT}/src',  # double quote の末尾 `$`
        f"grep -rl --include='*.py' --exclude-dir='node_*' {EVAL_SYM} {ROOT}/src",
        f"find {ROOT}/src -type f -name '*.py' -name '{EVAL_FRAG}*'",
        f'find {ROOT}/src -type f -name "{EVAL_FRAG}*"',
        f"grep -rl {EVAL_SYM} {ROOT}/src",  # literal 絶対 root path・unquoted は symbol のみ
    ],
)
def test_bash_quoted_patterns_and_literal_root_path_remain_eligible(command: str) -> None:
    parsed = NEW.parse_bash_search(command, ROOT)
    assert parsed["reason"] is None and parsed["scope_violation"] is None, (command, parsed)


def test_bash_literal_absolute_root_path_operand_is_still_eligible() -> None:
    for operand in (ROOT, f"{ROOT}/{SYN}", f"{ROOT}/.claude/skills/x-y_z.d", f"'{ROOT}/{SYN}'"):
        for command in (f"grep -rl {EVAL_SYM} {operand}", f"find {operand} -type f -name '{EVAL_FRAG}*'"):
            parsed = NEW.parse_bash_search(command, ROOT)
            assert parsed["reason"] is None and parsed["scope_violation"] is None, (command, parsed)

    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.bash(f"grep -rl {EVAL_SYM} {ROOT}/{SYN}", _abs_lines(EVAL_PATH))

    assert_outcome(evaluate(build("negative", mutate)), "pass", 5)


@pytest.mark.parametrize(
    "command",
    [
        f"grep -rl -E 'a.*b|c?d|[xy]+|{{1,2}}|^\\$' {ROOT}",  # regex の meta 文字（pattern 位置）
        f"grep -rl -e '*{EVAL_SYM}*' -e '$HOME' {ROOT}",  # `-e` pattern
        f"grep -rl --include='*.py' --exclude-dir='node_*' {EVAL_SYM} {ROOT}",  # flag の glob
        f"grep -rl '{EVAL_SYM}[0-9]*' {ROOT}",  # positional pattern
        f"find {ROOT} -type f -name '*{EVAL_FRAG}?*.py' -o -iname '[a-z]{{x,y}}*'",  # `-name` / `-iname` の glob
        f"find {ROOT} -type f -path '*/{EVAL_FRAG}/*'",  # `-path` の glob
    ],
)
def test_bash_pattern_position_regex_and_glob_are_not_misjudged_as_path_expansion(command: str) -> None:
    parsed = NEW.parse_bash_search(command, ROOT)
    assert parsed["reason"] is None, (command, parsed)
    assert parsed["scope_violation"] is None, (command, parsed)


def test_bash_non_string_or_empty_command_is_not_eligible() -> None:
    for command in (None, "", "   ", 5, ["grep"]):
        assert NEW.parse_bash_search(command, ROOT)["reason"] == "not_find_grep"


def test_bash_root_and_head_commands_are_exact_non_discovery() -> None:
    # root / HEAD と同一形状でも、別 path / 追加 flag / 連結は allowlist 外（search call として数える）。
    for command in (
        "git -C /synthetic/other rev-parse --show-toplevel",
        f"git -C {INV} rev-parse --show-toplevel --verify",
        f"git -C {INV} rev-parse --show-toplevel;ls",
        f"git -C {ROOT} rev-parse --short HEAD",
        f"git -C {ROOT} rev-parse HEAD | cat",
    ):

        def mutate(s: Stream, command: str = command) -> None:
            discovery_steps(s, "negative")
            s.bash(command, ROOT)

        assert_outcome(evaluate(build("negative", mutate)), "fail", 4, "bash_not_allowed")


# --- 規則 5: verdict ----------------------------------------------------------------------------------------


def test_rule5_negative_and_hybrid_require_all_target_paths_and_tested_head_in_the_high_union() -> None:
    for kind, roles in (("negative", NEG_ROLES), ("hybrid", HYB_ROLES)):
        for drop in roles:
            refs = [f"HEAD {HEAD}", *(r["path"] for name, r in roles.items() if name != drop)]
            result = {"assessment": "findings", "findings": [_finding("high", refs)]}
            assert_outcome(evaluate(normal_stream(kind, result), kind, result=result), "fail", 5)
        no_head = {"assessment": "findings", "findings": [_finding("blocker", [r["path"] for r in roles.values()])]}
        assert_outcome(evaluate(normal_stream(kind, no_head), kind, result=no_head), "fail", 5)
        medium = {
            "assessment": "findings",
            "findings": [_finding("medium", [HEAD, *(r["path"] for r in roles.values())])],
        }
        assert_outcome(evaluate(normal_stream(kind, medium), kind, result=medium), "fail", 5)
        assert_outcome(evaluate(normal_stream(kind, CLEAR), kind, result=CLEAR), "fail", 5)
        split = {
            "assessment": "findings",
            "findings": [
                _finding("high", [HEAD, roles["evaluator"]["path"]]),
                _finding("blocker", [r["path"] for name, r in roles.items() if name != "evaluator"]),
            ],
        }
        assert_outcome(evaluate(normal_stream(kind, split), kind, result=split), "pass", 5)


@pytest.mark.parametrize("lane", ["grep", "bash_grep", "bash_find"])
def test_rule5_positive_with_any_high_or_blocker_finding_is_fail(lane: str) -> None:
    for severity in ("high", "blocker"):
        over = {"assessment": "findings", "findings": [_finding(severity, ["symbol_name_only"])]}
        assert_outcome(evaluate(normal_stream("positive", over, lane), "positive", result=over), "fail", 5)
    medium = {"assessment": "findings", "findings": [_finding("medium", ["x"]), _finding("low", ["y"])]}
    assert_outcome(evaluate(normal_stream("positive", medium, lane), "positive", result=medium), "pass", 5)
    assert_outcome(evaluate(normal_stream("positive", CLEAR, lane), "positive", result=CLEAR), "pass", 5)


def test_rule5_unstructured_or_ambiguous_raw_result_is_fail() -> None:
    for raw in (None, "", "looks fine", {"assessment": "maybe", "findings": []}, {"assessment": "clear"}):
        assert_outcome(evaluate(normal_stream("negative"), result=raw), "fail", 5)
    malformed = {"assessment": "findings", "findings": [{"severity": "high", "evidence_refs": "not-a-list"}]}
    assert_outcome(evaluate(normal_stream("negative"), result=malformed), "fail", 5)


def test_unknown_fixture_kind_and_missing_roles_are_fail_closed() -> None:
    assert_outcome(evaluate(normal_stream(), kind="negative", fixture_kind="bogus"), "fail", 5)
    assert_outcome(evaluate(normal_stream(), fixture_roles=None), "fail", 5)


# --- simple ---------------------------------------------------------------------------------------------------


def test_simple_requires_no_discovery_and_no_source_read_beyond_allowed_paths() -> None:
    def with_grep(s: Stream) -> None:
        s.bundle()
        s.grep("anything", "x")

    assert_outcome(evaluate(build("simple", with_grep), "simple"), "fail", 4, "simple_discovery:Grep")

    def with_glob(s: Stream) -> None:
        s.bundle()
        s.glob("**/*.md", "x")

    assert_outcome(evaluate(build("simple", with_glob), "simple"), "fail", 4, "simple_discovery:Glob")

    def with_bash_grep(s: Stream) -> None:
        s.bundle()
        s.bash_grep("anything", _abs_lines(ALLOWED[0]))

    assert_outcome(evaluate(build("simple", with_bash_grep), "simple"), "fail", 4, "simple_discovery:Bash")

    def with_bash_find(s: Stream) -> None:
        s.bundle()
        s.bash(f"find {ROOT} -name '*.md'", _abs_lines(ALLOWED[0]))

    assert_outcome(evaluate(build("simple", with_bash_find), "simple"), "fail", 4, "simple_discovery:Bash")

    def with_other_bash(s: Stream) -> None:
        s.bundle()
        s.bash(f"cat {ROOT}/{ALLOWED[0]}", "glossary")

    assert_outcome(evaluate(build("simple", with_other_bash), "simple"), "fail", 4, "simple_bash_not_allowed")

    def with_source_read(s: Stream) -> None:
        s.bundle()
        s.read_path(NEG_ROLES["evaluator"]["path"])

    assert_outcome(evaluate(build("simple", with_source_read), "simple"), "fail", 4, "simple_source_read")

    def only_bundle(s: Stream) -> None:
        s.bundle()
        s.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")

    assert_outcome(evaluate(build("simple", only_bundle), "simple"), "pass", 5)

    def root_head_only(s: Stream) -> None:  # 許可された root / HEAD 解決 Bash は non-discovery（simple でも許容）
        s.root().head().bundle()

    assert_outcome(evaluate(build("simple", root_head_only), "simple"), "pass", 5)

    def allowed(s: Stream) -> None:
        s.bundle()
        s.read_path(ALLOWED[0], "glossary")

    assert_outcome(evaluate(build("simple", allowed), "simple"), "pass", 5)
    # allowed_read_paths を空にすると同じ Read は違反になる（allow-list が効いている）。
    assert_outcome(evaluate(build("simple", allowed), "simple", allowed_read_paths=[]), "fail", 4, "simple_source_read")


def test_simple_with_high_or_blocker_is_fail() -> None:
    for severity in ("high", "blocker"):
        over = {"assessment": "findings", "findings": [_finding(severity, ["x.md"])]}
        assert_outcome(evaluate(normal_stream("simple", over), "simple", result=over), "fail", 5)
    medium = {"assessment": "findings", "findings": [_finding("medium", ["x.md"])]}
    assert_outcome(evaluate(normal_stream("simple", medium), "simple", result=medium), "pass", 5)


def test_simple_denied_discovery_attempt_is_not_a_required_observation() -> None:
    """simple には必須観測が無いため、Grep / Bash grep の拒否は unavailable ではなく discovery 実行（fail）。"""
    s = Stream().init().agent_call().start().bundle()
    s.grep("anything", "denied", tool_use_id="toolu_g", is_error=True)
    s.stop().agent_result().final([{"tool_name": "Grep", "tool_use_id": "toolu_g"}])
    assert_outcome(evaluate(s, "simple"), "fail", 4, "simple_discovery:Grep")
    b = Stream().init().agent_call().start().bundle()
    b.bash_grep("anything", "denied", tool_use_id="toolu_b", is_error=True)
    b.stop().agent_result().final([{"tool_name": "Bash", "tool_use_id": "toolu_b"}])
    assert_outcome(evaluate(b, "simple"), "fail", 4, "simple_discovery:Bash")


# --- raw result 抽出と artifact 要約 ------------------------------------------------------------------------------


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("kind", ["negative", "positive", "hybrid"])
def test_pipeline_extracted_raw_result_feeds_the_evaluator(kind: str, lane: str) -> None:
    stream = normal_stream(kind, lane=lane)
    raw = NEW.extract_reviewer_raw_result(stream.text())
    assert raw is not None
    assert_outcome(evaluate(stream, kind, result=raw), "pass", 5)


def test_pipeline_extracted_raw_result_feeds_the_evaluator_for_simple() -> None:
    stream = normal_stream("simple")
    raw = NEW.extract_reviewer_raw_result(stream.text())
    assert raw is not None
    assert_outcome(evaluate(stream, "simple", result=raw), "pass", 5)


def test_artifact_records_per_tool_use_lane_classification_and_rule() -> None:
    mixed = Stream().init(["Read", "Bash", "Agent"]).agent_call().start()
    _prefix(mixed)
    mixed.grep(EVAL_SYM, _hit_text(EVAL_PATH))
    mixed.bash_grep(NEG_ROLES["consumer"]["symbol"], _abs_lines(NEG_ROLES["consumer"]["path"]))
    mixed.bash(f"rg -n x {ROOT}", "")
    mixed.read_role(NEG_ROLES, "consumer")
    mixed.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    outcome = evaluate(mixed)
    assert outcome["session_tools"] == ["Read", "Bash", "Agent"]
    by_rule = {}
    for record in outcome["tool_use_records"]:
        if "classification" in record:
            by_rule.setdefault(record["classification"]["rule"], []).append(record["classification"])
    assert by_rule["dedicated_grep_glob_tool"][0]["classification"] == "dedicated_lane_discovery"
    assert by_rule["dedicated_grep_glob_tool"][0]["lane"] == "dedicated"
    assert by_rule["bash_find_grep_eligible_shape"][0]["classification"] == "bash_lane_discovery"
    assert by_rule["bash_find_grep_eligible_shape"][0]["lane"] == "bash"
    assert by_rule["bash_not_allowlisted:not_find_grep"][0]["classification"] == "non_discovery"
    assert by_rule["bash_not_allowlisted:not_find_grep"][0]["counted_as_search"] is True
    assert by_rule["bash_exact_root_command"][0]["classification"] == "non_discovery"
    assert by_rule["bash_exact_head_command"][0]["counted_as_search"] is False
    assert by_rule["read_exempt_bundle_or_body"] and by_rule["read_repository_source"]
    # error の eligible call（session に無い専用 Grep の `No such tool available` 等）は attribution_eligible: false。
    assert by_rule["dedicated_grep_glob_tool"][0]["attribution_eligible"] is True
    assert by_rule["bash_find_grep_eligible_shape"][0]["attribution_eligible"] is True
    assert by_rule["bash_not_allowlisted:not_find_grep"][0]["attribution_eligible"] is False
    errored = Stream().init(["Read", "Bash", "Agent"]).agent_call().start()
    _prefix(errored)
    errored.grep(
        _all_symbols_pattern(NEG_ROLES), "<tool_use_error>No such tool available: Grep</tool_use_error>", is_error=True
    )
    do_search(errored, "bash_grep", NEG_ROLES, [r["path"] for r in NEG_ROLES.values()])
    _read_all_targets(errored)
    errored.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    errored_outcome = evaluate(errored)
    assert_outcome(errored_outcome, "pass", 5)  # 専用 Grep が使えず Bash lane で続行した場合も PASS しうる
    grep_record = next(r for r in errored_outcome["evidence"]["tool_use_classification"] if r["name"] == "Grep")
    assert grep_record["classification"] == "dedicated_lane_discovery" and grep_record["result_is_error"] is True
    assert grep_record["attribution_eligible"] is False and grep_record["counted_as_search"] is True
    assert errored_outcome["evidence"]["discovery"]["search_calls"] == 2
    assert errored_outcome["evidence"]["discovery"]["lanes_used"] == ["bash"]  # error の Grep は lane の根拠にしない
    # 判定が規則 4 以前で確定する stream でも、reviewer 帰属の tool_use の分類を残す（診断用）。
    early = Stream().init(["Read", "Bash"]).agent_call().start()
    early.bash_grep(EVAL_SYM, _abs_lines(EVAL_PATH))
    early_outcome = evaluate(early.stop().agent_result().final(denials=[{"tool_name": "Bash", "tool_use_id": "x"}]))
    assert any("classification" in r for r in early_outcome["tool_use_records"])


def test_artifact_tool_use_summary_includes_search_tools_and_is_sanitized() -> None:
    outcome = evaluate(normal_stream("negative"))
    records = [r for r in outcome["tool_use_records"] if r["name"] in ("Grep", "Glob", "Read")]
    grep = next(r for r in records if r["name"] == "Grep")
    assert set(grep["input_summary"]) == {"pattern", "path"} and grep["input_summary"]["path"] == "<ROOT>"
    bash_outcome = evaluate(normal_stream("negative", lane="bash_grep"))
    bash = [r for r in bash_outcome["tool_use_records"] if r["name"] == "Bash" and "grep" in r.get("input_summary", "")]
    assert bash and "<ROOT>" in bash[0]["input_summary"]
    dumped = json.dumps([*outcome["tool_use_records"], *bash_outcome["tool_use_records"]])
    assert ROOT not in dumped and "/home/" not in dumped
    home = Stream().init().agent_call().start()
    home.grep("fn_evaluator_zz", "/ho" "me/someone/secret/path.py", path=f"{ROOT}/x")
    summary = NEW.summarize_tool_uses(home.stop().agent_result().final().text(), ROOT, INV)
    assert "/home/" not in json.dumps(summary) and "<HOME>" in json.dumps(summary)


@pytest.mark.parametrize("kind", ["negative", "positive"])
def test_fixture_wide_shared_prefix_queries_observed_in_live_runs_remain_irrelevant_violations(kind: str) -> None:
    """live run で観測された Glob `**/*<prefix>*` と find `-name "*<prefix>*"` は、fixture 全体で共通する prefix
    だけの query であり、どの未解決 role の file 名断片でもないため違反のまま（evaluator を緩めない）。"""
    prefix = NEW.FIXTURE_DIRS[kind][1]
    roles = NEW.fixture_roles(kind)
    unresolved = NEW._unresolved_roles(roles)
    assert unresolved
    for role in unresolved.values():
        assert prefix in role["file_fragment"] and role["file_fragment"] != prefix

    def classify(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        tool_use = {"id": "toolu_x", "name": name, "stream_index": 1, "input": tool_input}
        return NEW.classify_interval_tool_use(
            tool_use, resolved_root=ROOT, invocation_dir=INV, body_file="body.md", unresolved_roles=unresolved
        )

    glob = classify("Glob", {"path": ROOT, "pattern": f"**/*{prefix}*"})
    assert glob["violation"] == "irrelevant_query:Glob" and glob["counted_as_search"] is True
    assert glob["classification"] == "non_discovery"
    find = classify("Bash", {"command": f'find {ROOT} -type f -name "*{prefix}*"'})
    assert find["violation"] == "irrelevant_query:Bash:find" and find["counted_as_search"] is True
    assert find["classification"] == "non_discovery"
    # 対照: role の file 名断片を verbatim で含めれば関連する検索として認定される。
    fragment = next(iter(unresolved.values()))["file_fragment"]
    assert classify("Glob", {"path": ROOT, "pattern": f"**/{fragment}*"})["violation"] is None
    assert classify("Bash", {"command": f'find {ROOT} -type f -name "{fragment}*"'})["violation"] is None
    symbol = next(iter(unresolved.values()))["symbol"]
    eligible_grep = classify("Bash", {"command": f"grep -rl --include='*.py' {symbol} {ROOT}"})
    assert eligible_grep["violation"] is None and eligible_grep["classification"] == "bash_lane_discovery"


@pytest.mark.parametrize("lane", ["grep", "bash_grep"])
def test_reading_the_consumer_without_searching_for_it_is_read_before_discovery(lane: str) -> None:
    """live run で観測: consumer を検索せず、他 role の検索だけで推測した consumer path を Read した。

    consumer も未解決 role で、検索結果にも既読 source の import / call-site にも根拠が無い
    推測 Read は違反のまま（evaluator を緩めない）。"""

    def guessed_consumer(s: Stream) -> None:
        _prefix(s)
        for role in ("producer", "evaluator", "parser"):
            symbol, path = NEG_ROLES[role]["symbol"], NEG_ROLES[role]["path"]
            if lane == "grep":
                s.grep(symbol, _hit_text(path))
            else:
                s.bash_grep(symbol, _abs_lines(path), flags="-rl")
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role, output="no import hints")

    outcome = evaluate(build("negative", guessed_consumer))
    assert_outcome(outcome, "fail", 4, "read_before_discovery:consumer")
    assert "missing_target_read:consumer" in outcome["evidence"]["violations"]


def test_one_alternation_grep_covering_every_unresolved_role_is_accepted() -> None:
    """prompt が許可する alternation の 1 回まとめ grep は全未解決 role を解決する。"""

    def alternation(s: Stream) -> None:
        _prefix(s)
        pattern = _all_symbols_pattern(NEG_ROLES)
        paths = _abs_lines(*(r["path"] for r in NEG_ROLES.values()), DECOY)
        s.bash(f"grep -rlE --include='*.py' '{pattern}' {ROOT}", paths)
        _read_all_targets(s)
        s.read_path(DECOY, "decoy")

    outcome = evaluate(build("negative", alternation))
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["discovery"]["search_calls"] == 1
