"""Issue #2963 AC8: `issue-design-reviewer` の runtime 結果を判定する pure evaluator。

Issue 本文「AC8 判定規則」だけを実装する。構造化 field のみを使い、自由文は解釈せず、LLM judge を使わない。

入力は harness が captured した stream-json の stdout 行そのもの（hermetic fixture も同じ行形式）、
`tested_head`、`resolved_root`、`invocation_dir`、fixture 種別、repository 相対の producer / parser /
evaluator / consumer path、reviewer の raw result である。抽出は既存 helper だけを使う（変更せず、unique module 名の
`importlib.util.spec_from_file_location` で読み込む）。

- lifecycle: `extract_claude_hook_lifecycle_events`
- permission 拒否: `extract_claude_permission_denials`
- tool 記録: assistant `tool_use`（`id` / `name` / `input`）と user `tool_result`
  （`tool_use_id` / `is_error` / `content`）block の走査（形状は
  `scripts/agent-ops/tests/_claude_gpt_role_subagent_smoke.py` が実測依拠するもの）

出力は `pass` / `fail` / `unavailable` と確定した規則番号であり、新しい汎用 parser / runner / registry は持たない。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

REVIEWER_AGENT = "issue-design-reviewer"
FIXTURE_KINDS = ("negative", "positive", "simple")
# producer / parser / evaluator / consumer の 4 役（Issue AC8 判定規則。evaluator の Read 欠落も規則 4 の FAIL）。
SOURCE_ROLES = ("producer", "parser", "evaluator", "consumer")
EXIT_CODES = {"pass": 0, "fail": 1, "unavailable": 77}
_HIGH_SEVERITIES = frozenset({"high", "blocker"})
_SHA_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_RUNNER_MODULE_NAME = "issue2963_run_worktree_agent_runtime_smoke"
# Stage 1b-4: the full live runner is Stage 3; only its pure stream extractors are migrated.
_RUNNER_PATH = Path(__file__).resolve().parents[4] / "scripts" / "agent-ops" / "claude_stream_extractors.py"


def load_runner_module() -> Any:
    """既存 runner を変更せず、unique module 名で一度だけ読み込む。"""
    cached = sys.modules.get(_RUNNER_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_RUNNER_MODULE_NAME, _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_RUNNER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# stream-json 走査（tool_use / tool_result）
# ---------------------------------------------------------------------------


def iter_stream_events(stdout: str) -> list[dict[str, Any]]:
    """JSON object に parse できる stream 行だけを順に返す（lifecycle helper の stream_index と同じ採番）。"""
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _result_text(block: dict[str, Any]) -> str:
    body = block.get("content")
    if isinstance(body, list):
        return "\n".join(str(item.get("text", "")) for item in body if isinstance(item, dict))
    return str(body or "")


def scan_tool_records(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """(tool_use 一覧, tool_use_id -> tool_result) を返す。tool_use は stream 位置と parent を持つ。"""
    tool_uses: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        for block in _content_blocks(event):
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                tool_input = block.get("input")
                tool_uses.append(
                    {
                        "id": block["id"],
                        "name": block.get("name"),
                        "input": tool_input if isinstance(tool_input, dict) else {},
                        "parent_tool_use_id": event.get("parent_tool_use_id"),
                        "stream_index": index,
                    }
                )
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                results[block["tool_use_id"]] = {
                    "is_error": bool(block.get("is_error")),
                    "text": _result_text(block),
                    "stream_index": index,
                }
    return tool_uses, results


def extract_reviewer_raw_result(stdout: str) -> str | None:
    """reviewer の hand-back text を構造化 record から取る（`SubagentHandback` tool_use の message、無ければ
    reviewer 型 SubagentStop の `last_assistant_message`）。自由文の解釈はしない。"""
    events = iter_stream_events(stdout)
    tool_uses, results = scan_tool_records(events)
    agent_ids = _reviewer_agent_tool_use_ids(tool_uses)
    for tool_use in tool_uses:
        if tool_use["name"] != "SubagentHandback" or tool_use["parent_tool_use_id"] not in agent_ids:
            continue
        message = tool_use["input"].get("message")
        result = results.get(tool_use["id"])
        if isinstance(message, str) and message.strip() and result is not None and not result["is_error"]:
            return message
    runner = load_runner_module()
    for entry in runner.extract_claude_hook_lifecycle_events(stdout):
        if (
            entry.get("hook_event") == "SubagentStop"
            and entry.get("agent_type") == REVIEWER_AGENT
            and isinstance(entry.get("last_assistant_message"), str)
            and entry["last_assistant_message"].strip()
        ):
            return entry["last_assistant_message"]
    return None


def parse_raw_result_object(raw_result: Any) -> dict[str, Any] | None:
    """raw result（dict、または JSON object を含む text）から `assessment` を持つ object を取り出す。"""
    if isinstance(raw_result, dict):
        return raw_result if "assessment" in raw_result else None
    if not isinstance(raw_result, str):
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw_result):
        try:
            candidate, _end = decoder.raw_decode(raw_result[match.start() :])
        except ValueError:
            continue
        if isinstance(candidate, dict) and "assessment" in candidate:
            return candidate
    return None


# ---------------------------------------------------------------------------
# reviewer 区間
# ---------------------------------------------------------------------------


def _reviewer_agent_tool_use_ids(tool_uses: list[dict[str, Any]]) -> set[str]:
    return {
        tool_use["id"]
        for tool_use in tool_uses
        if tool_use["name"] in ("Agent", "Task") and tool_use["input"].get("subagent_type") == REVIEWER_AGENT
    }


def _same_path(left: str, right: str) -> bool:
    return os.path.realpath(left) == os.path.realpath(right)


def _bash_tokens(tool_use: dict[str, Any]) -> list[str] | None:
    if tool_use["name"] != "Bash":
        return None
    command = tool_use["input"].get("command")
    if not isinstance(command, str):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _is_root_command(tool_use: dict[str, Any], invocation_dir: str) -> bool:
    tokens = _bash_tokens(tool_use)
    return (
        tokens is not None
        and len(tokens) == 5
        and tokens[:2] == ["git", "-C"]
        and tokens[3:] == ["rev-parse", "--show-toplevel"]
        and _same_path(tokens[2], invocation_dir)
    )


def _is_head_command(tool_use: dict[str, Any], resolved_root: str) -> bool:
    tokens = _bash_tokens(tool_use)
    return (
        tokens is not None
        and len(tokens) == 5
        and tokens[:2] == ["git", "-C"]
        and tokens[3:] == ["rev-parse", "HEAD"]
        and _same_path(tokens[2], resolved_root)
    )


def _read_target(tool_use: dict[str, Any], resolved_root: str, repo_relative: str) -> bool:
    if tool_use["name"] != "Read":
        return False
    file_path = tool_use["input"].get("file_path")
    expected = f"{resolved_root.rstrip('/')}/{repo_relative}"
    return isinstance(file_path, str) and os.path.normpath(file_path) == os.path.normpath(expected)


def _required_observations(
    kind: str, invocation_dir: str, resolved_root: str, fixture_paths: dict[str, str] | None
) -> list[dict[str, Any]]:
    """fixture 種別ごとの必須 tool 観測（simple は課さない）。各要素は matcher と成功判定を持つ。"""
    if kind == "simple":
        return []
    observations: list[dict[str, Any]] = [
        {
            "name": "R-ROOT",
            "matches": lambda tu: _is_root_command(tu, invocation_dir),
            "stdout_ok": lambda text: bool(text.strip()) and _same_path(text.strip(), resolved_root),
        },
        {"name": "R-HEAD", "matches": lambda tu: _is_head_command(tu, resolved_root), "stdout_ok": None},
    ]
    for role in SOURCE_ROLES:
        relative = (fixture_paths or {}).get(role)
        observations.append(
            {
                "name": f"R-SRC:{role}",
                "matches": (lambda tu, rel=relative: bool(rel) and _read_target(tu, resolved_root, rel)),
                "stdout_ok": None,
            }
        )
    return observations


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _verdict(verdict: str, rule: int, reason: str, **evidence: Any) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "rule": rule,
        "reason": reason,
        "exit_code": EXIT_CODES[verdict],
        "evidence": evidence,
    }


def _normalize_ref(ref: str, resolved_root: str) -> str:
    text = ref.strip().replace(resolved_root.rstrip("/") + "/", "")
    while text.startswith("./"):
        text = text[2:]
    return re.sub(r":\d+$", "", text)


def _high_ref_union(findings: list[Any]) -> tuple[list[str] | None, int]:
    """high|blocker finding 全体の evidence_refs の和集合（medium / low は含めない）。曖昧なら None。"""
    high = [f for f in findings if isinstance(f, dict) and f.get("severity") in _HIGH_SEVERITIES]
    union: list[str] = []
    for finding in high:
        refs = finding.get("evidence_refs")
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            return None, len(high)
        union.extend(refs)
    return union, len(high)


def _reviewer_invocation(lifecycle: list[dict[str, Any]]) -> dict[str, Any]:
    """reviewer 型の lifecycle record から、実 invocation の identity（非空 `agent_id`）ごとの位置を集める。

    実 stream-json（Claude Code 2.1.291）では 1 回の SubAgent invocation が複数の record を生む: SubagentStart は
    `hook_started` 2 本 + `hook_response` 2 本、SubagentStop は `hook_started` 3 本 + `hook_response` 3 本で、
    `extract_claude_hook_lifecycle_events` はそれぞれを 1 entry とする。`agent_id` を持つのは hook stdin を
    echo する observer hook の `hook_response` だけで、`hook_started` や他の hook の response は `agent_id` が
    None である。そのため invocation の identity は「reviewer 型かつ非空 `agent_id` を持つ record」の distinct な
    `agent_id` で定義する。distinct な `agent_id` が複数ある場合は別 invocation の重複であり、規則 3 で FAIL する。
    """
    ids: dict[str, dict[str, set[str]]] = {"SubagentStart": {}, "SubagentStop": {}}
    indexes: dict[str, list[int]] = {"SubagentStart": [], "SubagentStop": []}
    for entry in lifecycle:
        event = entry.get("hook_event")
        agent_id = entry.get("agent_id")
        if event not in ids or entry.get("agent_type") != REVIEWER_AGENT or not agent_id:
            continue
        ids[event].setdefault(agent_id, set())
        indexes[event].append(entry["stream_index"])
    return {
        "start_ids": set(ids["SubagentStart"]),
        "stop_ids": set(ids["SubagentStop"]),
        # 空のときに min/max が例外にならないよう sentinel を入れる（lifecycle_ok は ids の判定で先に偽になる）。
        "start_indexes": indexes["SubagentStart"] or [-1],
        "stop_indexes": indexes["SubagentStop"] or [-1],
    }


def summarize_lifecycle_records(stdout: str) -> list[dict[str, Any]]:
    """SubagentStart / SubagentStop の raw record の sanitized な要約（診断用。判定規則には使わない）。

    `hook_started` / `hook_response` を区別し、HOME を含む path や payload は一切含めない。"""
    runner = load_runner_module()
    by_index = {e["stream_index"]: e for e in runner.extract_claude_hook_lifecycle_events(stdout)}
    records: list[dict[str, Any]] = []
    for index, event in enumerate(iter_stream_events(stdout)):
        if event.get("type") != "system" or event.get("hook_event") not in ("SubagentStart", "SubagentStop"):
            continue
        derived = by_index.get(index, {})
        records.append(
            {
                "stream_index": index,
                "hook_event": event.get("hook_event"),
                "subtype": event.get("subtype"),
                "hook_id": event.get("hook_id"),
                "agent_id": derived.get("agent_id"),
                "agent_type": derived.get("agent_type"),
            }
        )
    return records


def sanitize_text(text: str, resolved_root: str, invocation_dir: str) -> str:
    """絶対 path を placeholder へ置換する（invocation_dir は root 配下なので先に置換する）。HOME も残さない。"""
    out = str(text)
    for value, placeholder in ((invocation_dir, "<INVOCATION_DIR>"), (resolved_root, "<ROOT>")):
        if value:
            out = out.replace(value.rstrip("/"), placeholder)
    return re.sub(r"/home/[^/\s\"']+", "<HOME>", out)


def summarize_tool_uses(stdout: str, resolved_root: str = "", invocation_dir: str = "") -> list[dict[str, Any]]:
    """tool_use の sanitized な要約（診断用。判定規則には使わない）。

    全 tool_use に id / name / parent_tool_use_id / stream_index / result の is_error を付ける。reviewer 帰属
    （parent が reviewer の Agent tool_use）の Bash は command（sanitized）と stdout の先頭 200 文字（sanitized）、
    Read は `<ROOT>` へ相対化した file_path を添える。他の tool の input は含めない。"""
    tool_uses, results = scan_tool_records(iter_stream_events(stdout))
    reviewer_agent_ids = _reviewer_agent_tool_use_ids(tool_uses)
    summary: list[dict[str, Any]] = []
    for tu in tool_uses:
        record: dict[str, Any] = {
            "id": tu["id"],
            "name": tu["name"],
            "parent_tool_use_id": tu["parent_tool_use_id"],
            "stream_index": tu["stream_index"],
        }
        result = results.get(tu["id"])
        record["result_is_error"] = None if result is None else result["is_error"]
        if tu["parent_tool_use_id"] in reviewer_agent_ids:
            if tu["name"] == "Bash" and isinstance(tu["input"].get("command"), str):
                record["input_summary"] = sanitize_text(tu["input"]["command"], resolved_root, invocation_dir)
                if result is not None:
                    record["stdout_head"] = sanitize_text(result["text"], resolved_root, invocation_dir)[:200]
            elif tu["name"] == "Read" and isinstance(tu["input"].get("file_path"), str):
                record["input_summary"] = sanitize_text(tu["input"]["file_path"], resolved_root, invocation_dir)
        summary.append(record)
    return summary


def evaluate_reachability_runtime(**kwargs: Any) -> dict[str, Any]:
    """AC8 判定規則を評価し、診断用の sanitized な lifecycle / tool_use 要約を添えて返す。"""
    outcome = _evaluate_rules(**kwargs)
    stdout = kwargs.get("stdout") or ""
    if iter_stream_events(stdout):
        outcome["lifecycle_records"] = summarize_lifecycle_records(stdout)
        outcome["tool_use_records"] = summarize_tool_uses(
            stdout, kwargs.get("resolved_root") or "", kwargs.get("invocation_dir") or ""
        )
    return outcome


def _evaluate_rules(
    *,
    stdout: str,
    tested_head: str,
    resolved_root: str,
    invocation_dir: str,
    fixture_kind: str,
    fixture_paths: dict[str, str] | None,
    raw_result: Any,
    claude_unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """AC8 判定規則 1-5 をこの順で評価し、最初に該当した規則で確定する。"""
    if fixture_kind not in FIXTURE_KINDS:
        return _verdict("fail", 5, f"unknown fixture_kind: {fixture_kind!r}")

    events = iter_stream_events(stdout)
    # 規則 1: stream-json が得られない（claude 不在 / 認証不能 / spawn 失敗）。
    if claude_unavailable_reason or not events:
        return _verdict("unavailable", 1, claude_unavailable_reason or "no stream-json events captured")

    runner = load_runner_module()
    tool_uses, results = scan_tool_records(events)
    denials = runner.extract_claude_permission_denials(stdout)
    lifecycle = runner.extract_claude_hook_lifecycle_events(stdout)
    agent_tool_ids = _reviewer_agent_tool_use_ids(tool_uses)

    # reviewer 区間（lifecycle が確定している場合だけ strict に決まる）。
    invocation = _reviewer_invocation(lifecycle)
    starts = invocation["start_indexes"]
    stops = invocation["stop_indexes"]
    interval: list[dict[str, Any]] = []
    lifecycle_ok = (
        len(invocation["start_ids"]) == 1
        and invocation["start_ids"] == invocation["stop_ids"]
        and not any(e.get("contradictory") for e in lifecycle)
        and max(starts) < min(stops)
        and len(agent_tool_ids) == 1
    )
    if lifecycle_ok:
        parent_id = next(iter(agent_tool_ids))
        interval = [
            tu
            for tu in tool_uses
            if tu["parent_tool_use_id"] == parent_id and min(starts) < tu["stream_index"] < max(stops)
        ]

    required = _required_observations(fixture_kind, invocation_dir, resolved_root, fixture_paths)

    # 規則 2: permission 拒否は permission_denials の構造化記録だけで判定する（tool_result の文言は使わない）。
    def _denied(tool_use: dict[str, Any]) -> bool:
        for denial in denials:
            if not isinstance(denial, dict):
                continue
            if denial.get("tool_use_id"):
                if denial["tool_use_id"] == tool_use["id"]:
                    return True
            elif denial.get("tool_name") == tool_use["name"] and denial.get("tool_input") == tool_use["input"]:
                return True
        return False

    denied_targets = [tu for tu in tool_uses if tu["id"] in agent_tool_ids and _denied(tu)]
    denied_targets += [tu for tu in interval if any(obs["matches"](tu) for obs in required) and _denied(tu)]
    if denied_targets:
        return _verdict(
            "unavailable",
            2,
            "permission denial recorded for a parent Agent or required observation tool_use",
            denied_tool_use_ids=[tu["id"] for tu in denied_targets],
        )

    # 規則 3: lifecycle / reviewer 区間。
    if len(agent_tool_ids) != 1:
        return _verdict(
            "fail", 3, f"expected exactly 1 parent Agent tool_use for {REVIEWER_AGENT}, got {len(agent_tool_ids)}"
        )
    parent_result = results.get(next(iter(agent_tool_ids)))
    if parent_result is None or parent_result["is_error"]:
        return _verdict("fail", 3, "parent Agent tool_result is missing or is_error")
    if not lifecycle_ok:
        return _verdict(
            "fail",
            3,
            "reviewer SubagentStart/SubagentStop must be exactly one non-contradictory pair "
            "with the same agent_id (Start before Stop)",
            reviewer_start_agent_ids=sorted(invocation["start_ids"]),
            reviewer_stop_agent_ids=sorted(invocation["stop_ids"]),
        )
    if not interval:
        return _verdict("fail", 3, "no reviewer-attributed tool_use inside the SubagentStart/SubagentStop interval")

    # 規則 4: 必須 tool 観測（permission 拒否以外の失敗は unavailable にしない）。
    observed: dict[str, bool] = {}
    for obs in required:
        success = False
        for tool_use in interval:
            if not obs["matches"](tool_use):
                continue
            result = results.get(tool_use["id"])
            if result is None or result["is_error"]:
                continue
            if obs["name"] == "R-HEAD":
                if result["text"].strip() == tested_head:
                    success = True
            elif obs["stdout_ok"] is not None:
                if obs["stdout_ok"](result["text"]):
                    success = True
            else:
                success = True
        observed[obs["name"]] = success
    failed = [name for name, ok in observed.items() if not ok]
    evidence = {
        "lifecycle": {
            "agent_id": next(iter(invocation["start_ids"])),
            "start_stream_index": min(starts),
            "stop_stream_index": max(stops),
        },
        "interval_tool_uses": [
            {"id": tu["id"], "name": tu["name"], "stream_index": tu["stream_index"]} for tu in interval
        ],
        "required_observations": observed,
    }
    if failed:
        return _verdict("fail", 4, f"required tool observation missing or failed: {failed}", **evidence)

    # 規則 5: verdict（構造化 field のみ。曖昧さは FAIL）。
    parsed = parse_raw_result_object(raw_result)
    if (
        parsed is None
        or parsed.get("assessment") not in ("clear", "findings")
        or not isinstance(parsed.get("findings"), list)
    ):
        return _verdict("fail", 5, "raw result is not a structured assessment/findings object", **evidence)
    assessment = parsed["assessment"]
    findings = parsed["findings"]
    union, high_count = _high_ref_union(findings)
    if union is None:
        return _verdict("fail", 5, "high|blocker finding has malformed evidence_refs", **evidence)
    normalized = [_normalize_ref(ref, resolved_root) for ref in union]
    paths = fixture_paths or {}

    if fixture_kind == "negative":
        if assessment != "findings" or high_count < 1:
            return _verdict(
                "fail", 5, "negative: expected assessment findings with at least one high|blocker finding", **evidence
            )
        missing = [role for role in SOURCE_ROLES if not any(paths.get(role, "\0") in ref for ref in normalized)]
        if missing:
            return _verdict("fail", 5, f"negative: high|blocker evidence_refs union lacks paths {missing}", **evidence)
        if tested_head not in {token for ref in union for token in _SHA_RE.findall(ref)}:
            return _verdict("fail", 5, "negative: high|blocker evidence_refs union lacks tested_head", **evidence)
        return _verdict(
            "pass",
            5,
            "negative: dataflow gap reported with HEAD and producer/parser/evaluator/consumer refs",
            **evidence,
        )

    if fixture_kind == "positive":
        if assessment == "clear":
            return _verdict("pass", 5, "positive: clear (observation verified by rule 4)", **evidence)
        cited = [role for role in SOURCE_ROLES if any(paths.get(role, "\0") in ref for ref in normalized)]
        if cited:
            return _verdict(
                "fail", 5, f"positive: high|blocker findings cite the working fixture paths {cited}", **evidence
            )
        return _verdict("pass", 5, "positive: no high|blocker finding cites the fixture paths", **evidence)

    if high_count:
        return _verdict("fail", 5, "simple: unexpected high|blocker finding for a docs-only Issue", **evidence)
    return _verdict("pass", 5, "simple: no high|blocker finding", **evidence)
