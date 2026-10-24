"""Issue #2973 AC7 / AC8: path 未列挙 role の bounded discovery を判定する pure evaluator。

#2963 の `issue2963_reachability_runtime_evaluator.py`（変更しない）の scanner / helper を unique module 名で
読み込んで再利用し、bound / root scope / role 単位の因果 / query 関連性の predicate だけを追加する。
構造化 field（tool_use の `name` / `input`、tool_result の `is_error` / `content`、lifecycle record）だけで判定し、
自由文は解釈せず、LLM judge を使わない。新しい汎用 parser / runner / analyzer は持たない。

discovery lane は 2 つ（effective runtime tool pool に従う。frontmatter の tool 宣言は effective pool を保証しない）。

- dedicated lane: 専用 `Grep` / `Glob`。
- Bash lane: root 束縛の read-only な Bash `find` / `grep`（narrowly supported shape を `shlex` で判定する。
  汎用 shell parser ではなく、許可する単一 simple command の形だけを受理し、それ以外は fail closed）。

reviewer 区間の Bash は allowlist で判定する: exact な root 解決 command、exact な HEAD 解決 command、eligible な
find / grep だけが許可される。それ以外の Bash は discovery と認めず、search call として数えて違反とする。

判定規則（最初に該当した規則で確定する）:

1. stream-json が得られない（unavailable）。
2. reviewer 区間の必須 tool 呼び出し（root / HEAD / target source の Read / eligible な discovery）または
   親 Agent tool_use が permission 拒否された、または session の tool pool に supported discovery lane が
   一つも存在しない（Bash も専用 Grep / Glob も無い。unavailable。構造化 permission_denials と `system init` の
   `tools` だけで判定する）。専用 Grep / Glob が無いことだけでは unavailable にしない（Bash lane で続行できる）。
3. lifecycle / reviewer 区間 / terminal completion（fail）。#2963 の規則 3 と同一判定。
4. 必須 tool 観測と bound（fail）。root -> HEAD -> role 単位の（direct Read | discovery -> target Read）。
5. verdict（fail / pass）。fixture 種別ごと。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Issue 本文「固定する bound の値」（実装側の都合で変更しない。contract / static test と同一値）
# ---------------------------------------------------------------------------

# reviewer 区間の eligible な discovery tool_use（専用 Grep / Glob 1 件、または eligible な Bash find / grep 1 件を
# 1 call と数える）と、eligible 形状を満たさない Bash の合計（成功・失敗を問わず数える）。
DISCOVERY_SEARCH_CALL_MAX = 8
DISCOVERY_SOURCE_READ_MAX = 8  # reviewer 区間の Read のうち bundle.json と body_file 以外の合計
SEARCH_SCOPE = "repository_root_only"
DISCOVERY_TOOLS = ("Grep", "Glob")  # dedicated lane
DISCOVERY_BASH_LANE = ("find", "grep")  # Bash lane（eligible 形状のみ discovery として数える）

# Bash lane の narrowly supported shape（起動 prompt の文言と一対一に対応する。拡張しない）。
GREP_SHORT_FLAGS = frozenset("rRnilEFwHIe")  # `-e` は次 token を pattern として消費する（連結時は末尾のみ）
GREP_LONG_FLAG_PREFIXES = ("--include=", "--exclude-dir=")  # 値は `--flag=X` 形式のみ（分離形式は不適格）
FIND_VALUE_PRIMARIES = ("-type", "-name", "-iname", "-path", "-maxdepth")
FIND_FLAG_PRIMARIES = ("-o",)
_SHELL_PUNCTUATION = frozenset("();<>|&")

FIXTURE_KINDS = ("negative", "positive", "hybrid", "simple")
SOURCE_ROLES = ("producer", "parser", "evaluator", "consumer")
_GAP_KINDS = ("negative", "hybrid")  # dataflow gap を持つ fixture（high|blocker finding を期待する）

_EVAL2963_NAME = "issue2963_reachability_runtime_evaluator"
_EVAL2963_PATH = Path(__file__).resolve().parent / "issue2963_reachability_runtime_evaluator.py"


def _load_2963() -> Any:
    """#2963 の evaluator を変更せず、既存 test と同じ module 名で一度だけ読み込む。"""
    cached = sys.modules.get(_EVAL2963_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_EVAL2963_NAME, _EVAL2963_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_EVAL2963_NAME] = module
    spec.loader.exec_module(module)
    return module


E63 = _load_2963()
EXIT_CODES = E63.EXIT_CODES
REVIEWER_AGENT = E63.REVIEWER_AGENT
iter_stream_events = E63.iter_stream_events
scan_tool_records = E63.scan_tool_records
load_runner_module = E63.load_runner_module
parse_raw_result_object = E63.parse_raw_result_object
extract_reviewer_raw_result = E63.extract_reviewer_raw_result
sanitize_text = E63.sanitize_text
summarize_lifecycle_records = E63.summarize_lifecycle_records
_SHA_RE = E63._SHA_RE
_verdict = E63._verdict
_normalize_ref = E63._normalize_ref
_high_ref_union = E63._high_ref_union
_is_root_command = E63._is_root_command
_is_head_command = E63._is_head_command
_read_target = E63._read_target
_reviewer_invocation = E63._reviewer_invocation
_reviewer_agent_tool_use_ids = E63._reviewer_agent_tool_use_ids


# ---------------------------------------------------------------------------
# fixture ground truth（#2963 の `fixture_paths` を拡張しない、本 Issue 用の別 metadata）
# ---------------------------------------------------------------------------

FIXTURES_RELATIVE = ".claude/skills/issue-refinement-loop/tests/fixtures"
# fixture 種別 -> (fixture directory, source file / symbol の接頭辞)
FIXTURE_DIRS = {
    "negative": ("bounded_discovery_negative_case", "qneg"),
    "positive": ("bounded_discovery_positive_control_case", "qpos"),
    "hybrid": ("bounded_discovery_hybrid_case", "qhyb"),
    "simple": ("bounded_discovery_simple_docs_only_case", None),
}
_ROLE_NAMES = {
    "producer": ("collect_{p}_vc_rows", "{p}_producer"),
    "parser": ("split_{p}_vc_line", "{p}_parser"),
    "evaluator": ("judge_{p}_vc_requirement", "{p}_evaluator"),
    "consumer": ("route_{p}_vc_decision", "{p}_consumer"),
}
_HYBRID_LISTED = ("producer", "parser")


def fixture_roles(kind: str) -> dict[str, dict[str, Any]]:
    """role -> `{path, symbol, file_fragment, listed, decoy_path}`。simple は role を持たない（空）。"""
    directory, prefix = FIXTURE_DIRS[kind]
    if prefix is None:
        return {}
    roles: dict[str, dict[str, Any]] = {}
    for role, (symbol, fragment) in _ROLE_NAMES.items():
        decoy = f"{FIXTURES_RELATIVE}/{directory}/decoy/qneg_stale_evaluator.py"
        roles[role] = {
            "path": f"{FIXTURES_RELATIVE}/{directory}/{fragment.format(p=prefix)}.py",
            "symbol": symbol.format(p=prefix),
            "file_fragment": fragment.format(p=prefix),
            "listed": kind == "hybrid" and role in _HYBRID_LISTED,
            "decoy_path": decoy if (kind == "negative" and role == "evaluator") else None,
        }
    return roles


def fixture_allowed_read_paths(kind: str) -> list[str]:
    """simple fixture が Read してよい repository file（bound の対象外の bundle.json / body_file は含まない）。"""
    if kind != "simple":
        return []
    directory, _prefix = FIXTURE_DIRS[kind]
    return [f"{FIXTURES_RELATIVE}/{directory}/qsimple_glossary.md"]


# ---------------------------------------------------------------------------
# 小さな predicate
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _under_root(path: str, root: str) -> bool:
    """正規化後の `path` が resolved root 配下（root 自身を含む）にあるか。`..` による脱出は偽になる。"""
    root_n = _norm(root)
    path_n = _norm(path)
    return path_n == root_n or path_n.startswith(root_n.rstrip("/") + "/")


def _exempt_read(tool_use: dict[str, Any], invocation_dir: str, body_file: str) -> bool:
    """bound の対象外にする Read: `<invocation_dir>` 直下の `bundle.json` と `body_file` の 2 file のみ。"""
    if tool_use["name"] != "Read":
        return False
    file_path = tool_use["input"].get("file_path")
    if not isinstance(file_path, str):
        return False
    base = invocation_dir.rstrip("/")
    return _norm(file_path) in (_norm(f"{base}/bundle.json"), _norm(f"{base}/{body_file}"))


def _read_path_is(tool_use: dict[str, Any], resolved_root: str, repo_relative: str) -> bool:
    return _read_target(tool_use, resolved_root, repo_relative)


def search_scope_violation(tool_use: dict[str, Any], resolved_root: str) -> str | None:
    """専用 Grep / Glob の search scope 違反（`path` 省略・相対 path・root 外・`..` 脱出・root 外 pattern）の理由。"""
    path = tool_use["input"].get("path")
    if not isinstance(path, str) or not path.strip():
        return "search_path_omitted"
    if not os.path.isabs(path):
        return "search_path_not_absolute"
    if not _under_root(path, resolved_root):
        return "search_outside_root"
    if tool_use["name"] == "Glob":
        pattern = tool_use["input"].get("pattern")
        if isinstance(pattern, str) and pattern:
            candidate = pattern if os.path.isabs(pattern) else f"{path.rstrip('/')}/{pattern}"
            if not _under_root(candidate, resolved_root):
                return "search_outside_root"
    return None


# Bash の path operand に含まれていたら実行時 argv が変わりうる shell expansion の文字（fail closed）。
# `$`（parameter / variable expansion）、`{` `}`（brace expansion / `${...}`）、`*` `?` `[`（pathname globbing）、
# `~`（tilde expansion）。path operand のみに適用し、grep の pattern / `find -name` の glob には適用しない。
_PATH_OPERAND_EXPANSION_CHARS = frozenset("${}*?[~")


def _path_operand_has_expansion(path: str) -> bool:
    return any(char in _PATH_OPERAND_EXPANSION_CHARS for char in path)


def _path_operands_scope_violation(paths: list[str], resolved_root: str) -> str | None:
    """Bash find / grep の path operand（resolved root 配下の expansion-free な絶対 literal path でなければ違反）。

    shlex は shell expansion 前の token を返すため、expansion を含みうる operand（実行時 argv が root 外へ変わりうる）は
    字句判定を信用せず fail closed にする。"""
    if not paths:
        return "search_path_omitted"
    if any(_path_operand_has_expansion(path) for path in paths):
        return "search_path_shell_expansion"
    for path in paths:
        if not os.path.isabs(path):
            return "search_path_not_absolute"
        if ".." in Path(path).parts or not _under_root(path, resolved_root):
            return "search_outside_root"
    return None


def _absolute_pattern_outside_root(pattern: str, resolved_root: str) -> bool:
    """`find -path` の絶対 pattern が root 外（または `..` を含む）を指すか。glob 文字より前の literal 部分で判定。"""
    if not pattern.startswith("/"):
        return False
    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    return ".." in Path(literal).parts or not _under_root(literal.rstrip("/") or "/", resolved_root)


# ---------------------------------------------------------------------------
# Bash lane: narrowly supported shape（汎用 shell parser ではない。形を外れたら fail closed）
# ---------------------------------------------------------------------------


# quote の外にある場合に、実行時 argv を変えうる（brace expansion・`$IFS` の word splitting・pathname globbing・tilde
# expansion で operand を増減・改変しうる）shell expansion の文字。pattern / glob も quote して渡す（fail closed）。
_UNQUOTED_EXPANSION_CHARS = frozenset("${*?[")


def _unquoted_shell_hazard(command: str) -> str | None:
    """quote の外の改行・コマンド置換・shell expansion・行継続（backslash + 改行）を検出する。

    `;` `&&` `||` `|` `>` `<` `&` などの演算子は `shlex` の punctuation token として別に検出する。
    quote した pattern / glob（`'SymA|SymB'` `"foo$"` `-name '*.py'` `--include='*.py'`）は許可する。
    backslash で escape した文字は literal として許可する（path operand 側の token 検査が別に fail closed にする）。"""
    in_single = in_double = False
    index = 0
    while index < len(command):
        char = command[index]
        if in_single:
            in_single = char != "'"
        elif char == "\\":
            if command[index + 1 : index + 2] in ("\n", "\r"):
                return "line_continuation"  # `.\<改行>.` は shell が `..` へ連結する（single quote の外のみ）
            index += 1
        elif in_double:
            if char == '"':
                in_double = False
            elif char == "`" or command.startswith("$(", index):
                return "command_substitution"
        elif char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char in "\n\r":
            return "newline"
        elif char == "`" or command.startswith("$(", index):
            return "command_substitution"
        elif char in _UNQUOTED_EXPANSION_CHARS or (char == "~" and (index == 0 or command[index - 1].isspace())):
            return "shell_expansion"
        index += 1
    return None


def _tokenize_command(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _parse_grep(tokens: list[str]) -> dict[str, Any]:
    patterns: list[str] = []
    positionals: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            prefix = next((p for p in GREP_LONG_FLAG_PREFIXES if token.startswith(p)), None)
            if prefix is None or not token[len(prefix) :]:
                return {"reason": f"unsupported_flag:{token}"}
        elif token.startswith("-") and len(token) > 1:
            letters = token[1:]
            if not all(letter in GREP_SHORT_FLAGS for letter in letters):
                return {"reason": f"unsupported_flag:{token}"}
            if "e" in letters:
                if letters.index("e") != len(letters) - 1 or index + 1 >= len(tokens):
                    return {"reason": f"unsupported_flag:{token}"}
                index += 1
                patterns.append(tokens[index])
        else:
            positionals.append(token)
        index += 1
    if not patterns:
        if not positionals:
            return {"reason": "no_pattern"}
        patterns.append(positionals.pop(0))
    return {"reason": None, "texts": patterns, "kind": "symbol", "hit_mode": "grep", "paths": positionals}


def _parse_find(tokens: list[str], resolved_root: str) -> dict[str, Any]:
    index = 1
    paths: list[str] = []
    while index < len(tokens) and not tokens[index].startswith("-"):
        if tokens[index] == "!":
            return {"reason": "unsupported_primary:!"}
        paths.append(tokens[index])
        index += 1
    fragments: list[str] = []
    outside_pattern = False
    while index < len(tokens):
        token = tokens[index]
        if token in FIND_FLAG_PRIMARIES:
            index += 1
        elif token in FIND_VALUE_PRIMARIES and index + 1 < len(tokens):
            value = tokens[index + 1]
            if token == "-maxdepth" and not value.isdigit():
                return {"reason": f"unsupported_primary:{token}"}
            if token in ("-name", "-iname", "-path"):
                fragments.append(value)
            if token == "-path" and _absolute_pattern_outside_root(value, resolved_root):
                outside_pattern = True
            index += 2
        else:
            return {"reason": f"unsupported_primary:{token}"}
    return {
        "reason": None,
        "texts": fragments,
        "kind": "fragment",
        "hit_mode": "path",
        "paths": paths,
        "outside_pattern": outside_pattern,
    }


def parse_bash_search(command: Any, resolved_root: str) -> dict[str, Any]:
    """Bash command を、許可する narrowly supported shape（単一 simple command の find / grep）として分解する。

    `reason` が None でない場合は eligible 形状を満たさない（fail closed）。`reason` が None の場合も、path operand の
    scope と関連性は別に判定する（`scope_violation` を返す）。"""
    if not isinstance(command, str) or not command.strip():
        return {"reason": "not_find_grep", "cmd": None}
    hazard = _unquoted_shell_hazard(command)
    if hazard:
        return {"reason": hazard, "cmd": None}
    try:
        tokens = _tokenize_command(command)
    except ValueError:
        return {"reason": "tokenize_error", "cmd": None}
    # 空白なしの `a;b` / `a&&b` を含む unquoted の演算子は、punctuation だけからなる独立 token になる（fail closed）。
    if any(token and all(char in _SHELL_PUNCTUATION for char in token) for token in tokens):
        return {"reason": "compound_operator", "cmd": None}
    cmd = tokens[0] if tokens else None
    if cmd not in DISCOVERY_BASH_LANE:
        return {"reason": "not_find_grep", "cmd": None}
    parsed = _parse_grep(tokens) if cmd == "grep" else _parse_find(tokens, resolved_root)
    parsed["cmd"] = cmd
    if parsed["reason"] is None:
        parsed["scope_violation"] = (
            "search_outside_root"
            if parsed.get("outside_pattern")
            else _path_operands_scope_violation(parsed["paths"], resolved_root)
        )
    return parsed


# ---------------------------------------------------------------------------
# 検索の関連性・帰属
# ---------------------------------------------------------------------------


def _dedicated_query(tool_use: dict[str, Any]) -> dict[str, Any]:
    pattern = tool_use["input"].get("pattern")
    is_grep = tool_use["name"] == "Grep"
    return {
        "kind": "symbol" if is_grep else "fragment",
        "texts": [pattern] if isinstance(pattern, str) else [],
        "hit_mode": "grep" if is_grep else "path",
    }


def _query_matches_role(query: dict[str, Any], role: dict[str, Any]) -> bool:
    """grep 系は named symbol、Glob / find は file 名断片を query に含むこと（その role に関連する検索）。"""
    needle = role.get("symbol" if query["kind"] == "symbol" else "file_fragment")
    return bool(needle) and any(needle in text for text in query["texts"])


def result_lists_path(text: str, resolved_root: str, repo_relative: str, mode: str = "grep") -> bool:
    """検索結果 text に `repo_relative` が「hit した file の path」として構造的に現れるか。

    先頭が path の行だけを数える（root からの相対でも絶対でもよい）。`mode="grep"` では `<path>` 単独の行（`-l` /
    files_with_matches）と `<path>:` で始まる行（`-n` / content / count）、`mode="path"`（Glob / find）では
    `<path>` 単独の行だけを数える。他 file の本文や metadata が matched content として P_R を含むだけの行
    （body・test・evaluator 内の自己参照 literal hit）は、行頭が P_R でないため数えない。"""
    root_prefix = resolved_root.rstrip("/") + "/"
    tail = r"(?:$|:)" if mode == "grep" else r"$"
    pattern = re.compile(re.escape(repo_relative) + tail)
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(root_prefix):
            line = line[len(root_prefix) :]
        while line.startswith("./"):
            line = line[2:]
        if pattern.match(line):
            return True
    return False


def _module_name(repo_relative: str) -> str:
    return Path(repo_relative).stem


def text_mentions_module(text: str, repo_relative: str) -> bool:
    """既に Read した source の結果 text に `repo_relative` の module 名が語として現れるか（import / call-site）。"""
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(_module_name(repo_relative))}(?![A-Za-z0-9_])", text or "") is not None
    )


# ---------------------------------------------------------------------------
# tool_use の分類（lane / discovery か否か / 認定した規則）
# ---------------------------------------------------------------------------


def classify_interval_tool_use(
    tool_use: dict[str, Any],
    *,
    resolved_root: str,
    invocation_dir: str,
    body_file: str,
    unresolved_roles: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """reviewer 区間の 1 tool_use を分類する。

    `classification` は `dedicated_lane_discovery` / `bash_lane_discovery` / `non_discovery`、`rule` は認定した規則。
    `counted_as_search` は search bound に算入するか（専用 Grep / Glob と、root / HEAD 以外の全 Bash）。`violation` は
    contract 違反の code（`unresolved_roles` が None の場合は関連性を判定しない）。discovery と認定されるのは違反が
    無い eligible な検索だけである。"""
    name = tool_use["name"]
    record: dict[str, Any] = {
        "id": tool_use["id"],
        "name": name,
        "stream_index": tool_use["stream_index"],
        "lane": None,
        "classification": "non_discovery",
        "rule": "other_tool",
        "counted_as_search": False,
        "violation": None,
        "cmd": None,
        "query": None,
    }

    def relevance_violation(query: dict[str, Any]) -> bool:
        return unresolved_roles is not None and not any(
            _query_matches_role(query, role) for role in unresolved_roles.values()
        )

    if name in DISCOVERY_TOOLS:
        query = _dedicated_query(tool_use)
        record.update(lane="dedicated", counted_as_search=True, query=query, cmd=name.lower())
        scope = search_scope_violation(tool_use, resolved_root)
        if scope:
            record.update(violation=f"{scope}:{name}", rule=f"dedicated_{name.lower()}:{scope}")
        elif relevance_violation(query):
            record.update(violation=f"irrelevant_query:{name}", rule=f"dedicated_{name.lower()}:irrelevant_query")
        else:
            record.update(classification="dedicated_lane_discovery", rule="dedicated_grep_glob_tool")
        return record
    if name == "Bash":
        if _is_root_command(tool_use, invocation_dir):
            record["rule"] = "bash_exact_root_command"
            return record
        if _is_head_command(tool_use, resolved_root):
            record["rule"] = "bash_exact_head_command"
            return record
        parsed = parse_bash_search(tool_use["input"].get("command"), resolved_root)
        record.update(lane="bash", counted_as_search=True, cmd=parsed.get("cmd"))
        if parsed["reason"]:
            record.update(
                violation=f"bash_not_allowed:{parsed['reason']}", rule=f"bash_not_allowlisted:{parsed['reason']}"
            )
            return record
        label = f"Bash:{parsed['cmd']}"
        query = {"kind": parsed["kind"], "texts": parsed["texts"], "hit_mode": parsed["hit_mode"]}
        record["query"] = query
        if parsed["scope_violation"]:
            record.update(
                violation=f"{parsed['scope_violation']}:{label}",
                rule=f"bash_{parsed['cmd']}:{parsed['scope_violation']}",
            )
        elif relevance_violation(query):
            record.update(violation=f"irrelevant_query:{label}", rule=f"bash_{parsed['cmd']}:irrelevant_query")
        else:
            record.update(classification="bash_lane_discovery", rule="bash_find_grep_eligible_shape")
        return record
    if name == "Read":
        record["rule"] = (
            "read_exempt_bundle_or_body"
            if _exempt_read(tool_use, invocation_dir, body_file)
            else "read_repository_source"
        )
    return record


def classify_tool_uses(
    tool_uses: list[dict[str, Any]],
    *,
    resolved_root: str,
    invocation_dir: str,
    body_file: str,
    unresolved_roles: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [
        classify_interval_tool_use(
            tu,
            resolved_root=resolved_root,
            invocation_dir=invocation_dir,
            body_file=body_file,
            unresolved_roles=unresolved_roles,
        )
        for tu in sorted(tool_uses, key=lambda item: item["stream_index"])
    ]


# ---------------------------------------------------------------------------
# session の tool pool（`system init` の `tools`）
# ---------------------------------------------------------------------------


def session_tools(events: list[dict[str, Any]]) -> list[str] | None:
    """`system init` event の `tools`（構造化 field）。`init` に `tools` が無い stream では None（判定しない）。"""
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init" and isinstance(event.get("tools"), list):
            return [tool for tool in event["tools"] if isinstance(tool, str)]
    return None


def supported_discovery_lanes(tools: list[str]) -> list[str]:
    """session tool pool が提供する discovery lane（dedicated: Grep / Glob のいずれか、bash: Bash）。

    Claude Code の native build は専用 Grep / Glob を既定の tool pool から外し、discovery を Bash 経由の
    `find` / `grep` で行う。専用 lane が無いこと自体は unavailable ではない（Bash lane が使える）。"""
    lanes = []
    if any(tool in tools for tool in DISCOVERY_TOOLS):
        lanes.append("dedicated")
    if "Bash" in tools:
        lanes.append("bash")
    return lanes


# ---------------------------------------------------------------------------
# 規則 1-3（#2963 の規則 1-3 と同一判定。drift は parity test が検出する）
# ---------------------------------------------------------------------------


def _required_read_paths(fixture_roles: dict[str, dict[str, Any]] | None) -> list[str]:
    return [role["path"] for role in (fixture_roles or {}).values() if role.get("path")]


def evaluate_rules_1_3(
    *,
    stdout: str,
    resolved_root: str,
    invocation_dir: str,
    fixture_kind: str,
    fixture_roles: dict[str, dict[str, Any]] | None,
    claude_unavailable_reason: str | None = None,
    terminal_incomplete_reason: str | None = None,
    require_discovery_lane: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """規則 1-3 を評価する。(確定した outcome | None, 規則 4 以降が使う context) を返す。"""
    events = iter_stream_events(stdout)
    if claude_unavailable_reason or not events:
        return _verdict("unavailable", 1, claude_unavailable_reason or "no stream-json events captured"), {}

    if require_discovery_lane:
        tools = session_tools(events)
        if tools is not None and not supported_discovery_lanes(tools):
            return (
                _verdict(
                    "unavailable",
                    2,
                    "no supported discovery lane in the session tool pool (system init `tools` has neither Bash "
                    "nor dedicated Grep / Glob)",
                    session_tools=[tool for tool in tools if not tool.startswith("mcp__")],
                ),
                {},
            )

    runner = load_runner_module()
    tool_uses, results = scan_tool_records(events)
    denials = runner.extract_claude_permission_denials(stdout)
    lifecycle = runner.extract_claude_hook_lifecycle_events(stdout)
    agent_tool_ids = _reviewer_agent_tool_use_ids(tool_uses)

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

    required_reads = _required_read_paths(fixture_roles) if fixture_kind != "simple" else []

    def _required_observation(tool_use: dict[str, Any]) -> bool:
        if fixture_kind == "simple":
            return False
        if _is_root_command(tool_use, invocation_dir) or _is_head_command(tool_use, resolved_root):
            return True
        if tool_use["name"] in DISCOVERY_TOOLS:
            return True
        if tool_use["name"] == "Bash":  # eligible 形状の find / grep だけが discovery（他の Bash は必須観測ではない）
            return parse_bash_search(tool_use["input"].get("command"), resolved_root)["reason"] is None
        return any(_read_path_is(tool_use, resolved_root, rel) for rel in required_reads)

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
    denied_targets += [tu for tu in interval if _required_observation(tu) and _denied(tu)]
    if denied_targets:
        return (
            _verdict(
                "unavailable",
                2,
                "permission denial recorded for a parent Agent or required observation tool_use",
                denied_tool_use_ids=[tu["id"] for tu in denied_targets],
            ),
            {},
        )

    if len(agent_tool_ids) != 1:
        return (
            _verdict(
                "fail", 3, f"expected exactly 1 parent Agent tool_use for {REVIEWER_AGENT}, got {len(agent_tool_ids)}"
            ),
            {},
        )
    parent_result = results.get(next(iter(agent_tool_ids)))
    if parent_result is None or parent_result["is_error"]:
        return _verdict("fail", 3, "parent Agent tool_result is missing or is_error"), {}
    if not lifecycle_ok:
        return (
            _verdict(
                "fail",
                3,
                "reviewer SubagentStart/SubagentStop must be exactly one non-contradictory pair "
                "with the same agent_id (Start before Stop)",
                reviewer_start_agent_ids=sorted(invocation["start_ids"]),
                reviewer_stop_agent_ids=sorted(invocation["stop_ids"]),
            ),
            {},
        )
    if not interval:
        return _verdict("fail", 3, "no reviewer-attributed tool_use inside the SubagentStart/SubagentStop interval"), {}
    if terminal_incomplete_reason:
        return _verdict("fail", 3, f"terminal completion not reached: {terminal_incomplete_reason}"), {}

    context = {
        "tool_uses": tool_uses,
        "results": results,
        "interval": interval,
        "lifecycle": {
            "agent_id": next(iter(invocation["start_ids"])),
            "start_stream_index": min(starts),
            "stop_stream_index": max(stops),
        },
    }
    return None, context


def terminal_incomplete_reason(stdout: str, timed_out: bool) -> str | None:
    """terminal completion に至らなかった理由（timeout / turn limit / error result）。完了していれば None。

    最後の `result` event が `subtype: success` かつ非 error であることだけを構造化 field で判定する。"""
    if timed_out:
        return "timed out before terminal completion"
    last: dict[str, Any] | None = None
    for event in iter_stream_events(stdout):
        if event.get("type") == "result":
            last = event
    if last is None:
        return "no terminal result event"
    if last.get("subtype") != "success" or last.get("is_error"):
        return f"terminal result subtype={last.get('subtype')!r} is_error={bool(last.get('is_error'))}"
    return None


# ---------------------------------------------------------------------------
# 規則 4: 必須 tool 観測 / bound / scope / 因果
# ---------------------------------------------------------------------------


def _unresolved_roles(fixture_roles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {name: role for name, role in fixture_roles.items() if not role.get("listed")}


def _evaluate_simple_observations(
    *,
    classified: list[dict[str, Any]],
    interval: list[dict[str, Any]],
    resolved_root: str,
    invocation_dir: str,
    body_file: str,
    allowed_read_paths: list[str],
) -> list[str]:
    """simple は discovery（専用 Grep / Glob と Bash の find / grep）、許可されない Bash、source Read を持たない。"""
    violations: list[str] = []
    for record in classified:
        if record["name"] in DISCOVERY_TOOLS:
            violations.append(f"simple_discovery:{record['name']}")
        elif record["name"] == "Bash" and record["counted_as_search"]:
            violations.append("simple_discovery:Bash" if record["cmd"] else "simple_bash_not_allowed")
    for tool_use in interval:
        if tool_use["name"] == "Read" and not _exempt_read(tool_use, invocation_dir, body_file):
            if not any(_read_path_is(tool_use, resolved_root, rel) for rel in allowed_read_paths):
                violations.append("simple_source_read")
    return violations


def _evaluate_discovery_observations(
    *,
    classified: list[dict[str, Any]],
    interval: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    tested_head: str,
    resolved_root: str,
    invocation_dir: str,
    body_file: str,
    fixture_roles: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    """root -> HEAD -> role 単位の（direct Read | discovery -> target Read）と bound を検証する。"""
    violations: list[str] = []
    by_index = sorted(interval, key=lambda tu: tu["stream_index"])

    def _ok_results(predicate: Any) -> list[dict[str, Any]]:
        found = []
        for tool_use in by_index:
            result = results.get(tool_use["id"])
            if predicate(tool_use) and result is not None and not result["is_error"]:
                found.append({"tool_use": tool_use, "result": result})
        return found

    roots = [
        item
        for item in _ok_results(lambda tu: _is_root_command(tu, invocation_dir))
        if item["result"]["text"].strip() and E63._same_path(item["result"]["text"].strip(), resolved_root)
    ]
    heads = [
        item
        for item in _ok_results(lambda tu: _is_head_command(tu, resolved_root))
        if item["result"]["text"].strip() == tested_head
    ]
    if not roots:
        violations.append("root_resolution_missing")
    if not heads:
        violations.append("head_resolution_missing")
    head_use_index: int | None = None
    if roots and heads:
        root_result_index = min(r["result"]["stream_index"] for r in roots)
        later = [h for h in heads if h["tool_use"]["stream_index"] > root_result_index]
        if later:
            head_use_index = min(h["tool_use"]["stream_index"] for h in later)
        else:
            violations.append("head_before_root")

    # --- bound / scope / query 関連性 / allowlist --------------------------------------------------------------
    searches = [record for record in classified if record["counted_as_search"]]
    source_reads = [tu for tu in by_index if tu["name"] == "Read" and not _exempt_read(tu, invocation_dir, body_file)]
    if len(searches) > DISCOVERY_SEARCH_CALL_MAX:
        violations.append(f"search_bound_exceeded:{len(searches)}>{DISCOVERY_SEARCH_CALL_MAX}")
    if len(source_reads) > DISCOVERY_SOURCE_READ_MAX:
        violations.append(f"read_bound_exceeded:{len(source_reads)}>{DISCOVERY_SOURCE_READ_MAX}")
    violations.extend(record["violation"] for record in searches if record["violation"])
    unresolved = _unresolved_roles(fixture_roles)
    if head_use_index is not None:
        for stream_index, name in [
            *((record["stream_index"], record["name"]) for record in searches),
            *((tu["stream_index"], tu["name"]) for tu in source_reads),
        ]:
            if stream_index <= head_use_index:
                violations.append(f"before_head:{name}")
                break

    # --- role 単位の帰属（direct Read | (a) 成功した eligible discovery の hit | (b) import 由来） -------------------
    # is_error の eligible call は bound に算入されるが、discovery attribution には使わない。
    attributing = []
    for record in classified:
        result = results.get(record["id"])
        if (
            record["classification"] in ("dedicated_lane_discovery", "bash_lane_discovery")
            and result is not None
            and not result["is_error"]
        ):
            attributing.append({"record": record, "result": result})

    def _discovery_hits(item: dict[str, Any], role: dict[str, Any]) -> bool:
        query = item["record"]["query"]
        return _query_matches_role(query, role) and result_lists_path(
            item["result"]["text"], resolved_root, role["path"], query["hit_mode"]
        )

    role_state: dict[str, dict[str, Any]] = {}
    read_texts: list[tuple[int, str]] = []  # (result stream_index, text) of successfully Read target sources
    discovered: dict[str, bool] = {}
    for name, role in fixture_roles.items():
        reads = [tu for tu in by_index if _read_path_is(tu, resolved_root, role["path"])]
        state = {"listed": bool(role.get("listed")), "read_attempts": len(reads), "satisfied": False, "via": None}
        role_state[name] = state
        discovered[name] = any(_discovery_hits(item, role) for item in attributing)
    all_target_reads = sorted(
        (
            (tu["stream_index"], name, tu)
            for name, role in fixture_roles.items()
            for tu in by_index
            if _read_path_is(tu, resolved_root, role["path"])
        ),
        key=lambda item: item[0],
    )
    for read_index, name, tool_use in all_target_reads:
        role = fixture_roles[name]
        result = results.get(tool_use["id"])
        succeeded = result is not None and not result["is_error"]
        if role.get("listed"):
            justified, via = True, "direct_read"
        else:
            via = None
            for item in attributing:
                if item["result"]["stream_index"] < read_index and _discovery_hits(item, role):
                    via = "discovery"
                    break
            if via is None:
                for text_index, text in read_texts:
                    if text_index < read_index and text_mentions_module(text, role["path"]):
                        via = "import"
                        break
            justified = via is not None
            if not justified:
                violations.append(f"read_before_discovery:{name}")
        if succeeded:
            if justified and not role_state[name]["satisfied"]:
                role_state[name]["satisfied"] = True
                role_state[name]["via"] = via
            read_texts.append((result["stream_index"], result["text"]))
    for name, state in role_state.items():
        if not state["satisfied"]:
            violations.append(f"missing_target_read:{name}")

    # 少なくとも 1 件、未解決 role の target path を含む成功した discovery が必要（import 導出だけで済ませない）。
    if unresolved and not any(discovered[name] for name in unresolved):
        violations.append("no_successful_discovery")

    evidence = {
        "search_calls": len(searches),
        "source_reads": len(source_reads),
        "search_call_max": DISCOVERY_SEARCH_CALL_MAX,
        "source_read_max": DISCOVERY_SOURCE_READ_MAX,
        # attribution に使われた（非 error の）eligible discovery の lane。error の call は bound に算入されるだけ。
        "lanes_used": sorted({item["record"]["lane"] for item in attributing}),
        "roles": {
            name: {"listed": s["listed"], "satisfied": s["satisfied"], "via": s["via"]}
            for name, s in role_state.items()
        },
    }
    return sorted(set(violations)), evidence


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _public_classification(record: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """artifact 用: tool_use ごとの lane / 分類 / 認定した規則（`query` の中身は含めない）。

    `classification` は eligible な検索 call としての認定であり、result が error の call（例: session に無い専用
    Grep の `No such tool available`、grep の no match）は `attribution_eligible: false`（bound には算入されるが
    discovery attribution には使われない）として区別する。"""
    public = {
        key: record[key]
        for key in ("id", "name", "stream_index", "lane", "classification", "rule", "counted_as_search", "violation")
    }
    result = results.get(record["id"])
    public["result_is_error"] = None if result is None else result["is_error"]
    public["attribution_eligible"] = (
        record["classification"] in ("dedicated_lane_discovery", "bash_lane_discovery")
        and result is not None
        and not result["is_error"]
    )
    return public


def evaluate_bounded_discovery(
    *,
    stdout: str,
    tested_head: str,
    resolved_root: str,
    invocation_dir: str,
    fixture_kind: str,
    fixture_roles: dict[str, dict[str, Any]] | None,
    raw_result: Any,
    allowed_read_paths: list[str] | None = None,
    body_file: str = "body.md",
    claude_unavailable_reason: str | None = None,
    terminal_incomplete: str | None = None,
) -> dict[str, Any]:
    """AC7 判定規則 1-5 をこの順で評価する（最初に該当した規則で確定する）。"""
    if fixture_kind not in FIXTURE_KINDS:
        return _verdict("fail", 5, f"unknown fixture_kind: {fixture_kind!r}")
    if fixture_kind != "simple" and not fixture_roles:
        return _verdict("fail", 5, f"fixture_roles is required for fixture_kind {fixture_kind!r}")

    outcome, context = evaluate_rules_1_3(
        stdout=stdout,
        resolved_root=resolved_root,
        invocation_dir=invocation_dir,
        fixture_kind=fixture_kind,
        fixture_roles=fixture_roles,
        claude_unavailable_reason=claude_unavailable_reason,
        terminal_incomplete_reason=terminal_incomplete,
        require_discovery_lane=fixture_kind != "simple",
    )
    if outcome is not None:
        return outcome

    interval = context["interval"]
    results = context["results"]
    roles = fixture_roles or {}
    classified = classify_tool_uses(
        interval,
        resolved_root=resolved_root,
        invocation_dir=invocation_dir,
        body_file=body_file,
        unresolved_roles=None if fixture_kind == "simple" else _unresolved_roles(roles),
    )
    if fixture_kind == "simple":
        violations = _evaluate_simple_observations(
            classified=classified,
            interval=interval,
            resolved_root=resolved_root,
            invocation_dir=invocation_dir,
            body_file=body_file,
            allowed_read_paths=list(allowed_read_paths or []),
        )
        observation: dict[str, Any] = {"search_calls": sum(1 for r in classified if r["counted_as_search"])}
    else:
        violations, observation = _evaluate_discovery_observations(
            classified=classified,
            interval=interval,
            results=results,
            tested_head=tested_head,
            resolved_root=resolved_root,
            invocation_dir=invocation_dir,
            body_file=body_file,
            fixture_roles=roles,
        )
    evidence = {
        "lifecycle": context["lifecycle"],
        "interval_tool_uses": [
            {"id": tu["id"], "name": tu["name"], "stream_index": tu["stream_index"]} for tu in interval
        ],
        "tool_use_classification": [_public_classification(record, results) for record in classified],
        "discovery": observation,
        "violations": violations,
    }
    if violations:
        return _verdict("fail", 4, f"bounded discovery observation violated: {violations}", **evidence)

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

    if fixture_kind in _GAP_KINDS:
        if assessment != "findings" or high_count < 1:
            return _verdict(
                "fail",
                5,
                f"{fixture_kind}: expected assessment findings with at least one high|blocker finding",
                **evidence,
            )
        missing = [name for name, role in roles.items() if not any(role["path"] in ref for ref in normalized)]
        if missing:
            return _verdict(
                "fail", 5, f"{fixture_kind}: high|blocker evidence_refs union lacks target paths {missing}", **evidence
            )
        if tested_head not in {token for ref in union for token in _SHA_RE.findall(ref)}:
            return _verdict(
                "fail", 5, f"{fixture_kind}: high|blocker evidence_refs union lacks tested_head", **evidence
            )
        return _verdict(
            "pass",
            5,
            f"{fixture_kind}: dataflow gap reported with HEAD and the discovered/direct target source paths",
            **evidence,
        )

    if fixture_kind == "positive":
        if high_count:
            return _verdict("fail", 5, "positive: high|blocker finding present (must be 0)", **evidence)
        return _verdict("pass", 5, "positive: no high|blocker finding (observation verified by rule 4)", **evidence)

    if high_count:
        return _verdict("fail", 5, "simple: unexpected high|blocker finding for a docs-only Issue", **evidence)
    return _verdict("pass", 5, "simple: no discovery, no source Read, no high|blocker finding", **evidence)


# ---------------------------------------------------------------------------
# artifact 用の sanitized な要約（診断用。判定規則には使わない）
# ---------------------------------------------------------------------------


def summarize_tool_uses(
    stdout: str,
    resolved_root: str = "",
    invocation_dir: str = "",
    *,
    fixture_roles: dict[str, dict[str, Any]] | None = None,
    fixture_kind: str = "negative",
    body_file: str = "body.md",
) -> list[dict[str, Any]]:
    """tool_use の sanitized な要約。reviewer 帰属の tool_use には lane / 分類 / 認定した規則を付ける。

    reviewer 帰属の Bash / Read / Grep / Glob は input を sanitized で添える。分類は診断用であり、
    判定は `evaluate_bounded_discovery` が reviewer 区間（SubagentStart / Stop の間）で行う。"""
    tool_uses, results = scan_tool_records(iter_stream_events(stdout))
    reviewer_agent_ids = _reviewer_agent_tool_use_ids(tool_uses)
    attributed = [tu for tu in tool_uses if tu["parent_tool_use_id"] in reviewer_agent_ids]
    unresolved = None if fixture_kind == "simple" else _unresolved_roles(fixture_roles or {})
    classification = {
        record["id"]: record
        for record in classify_tool_uses(
            attributed,
            resolved_root=resolved_root,
            invocation_dir=invocation_dir,
            body_file=body_file,
            unresolved_roles=unresolved,
        )
    }
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
        if tu["id"] in classification:
            tool_input = tu["input"]
            record["classification"] = _public_classification(classification[tu["id"]], results)
            if tu["name"] == "Bash" and isinstance(tool_input.get("command"), str):
                record["input_summary"] = sanitize_text(tool_input["command"], resolved_root, invocation_dir)
                if result is not None:
                    record["stdout_head"] = sanitize_text(result["text"], resolved_root, invocation_dir)[:200]
            elif tu["name"] == "Read" and isinstance(tool_input.get("file_path"), str):
                record["input_summary"] = sanitize_text(tool_input["file_path"], resolved_root, invocation_dir)
            elif tu["name"] in DISCOVERY_TOOLS:
                record["input_summary"] = {
                    "pattern": sanitize_text(str(tool_input.get("pattern", "")), resolved_root, invocation_dir),
                    "path": sanitize_text(str(tool_input.get("path", "<omitted>")), resolved_root, invocation_dir),
                }
                if result is not None:
                    record["result_head"] = sanitize_text(result["text"], resolved_root, invocation_dir)[:300]
        summary.append(record)
    return summary


def evaluate_bounded_discovery_runtime(**kwargs: Any) -> dict[str, Any]:
    """判定に、診断用の sanitized な session tools / lifecycle / tool_use 要約を添えて返す。"""
    outcome = evaluate_bounded_discovery(**kwargs)
    stdout = kwargs.get("stdout") or ""
    events = iter_stream_events(stdout)
    if events:
        tools = session_tools(events)
        outcome["session_tools"] = None if tools is None else [t for t in tools if not t.startswith("mcp__")]
        outcome["lifecycle_records"] = summarize_lifecycle_records(stdout)
        outcome["tool_use_records"] = summarize_tool_uses(
            stdout,
            kwargs.get("resolved_root") or "",
            kwargs.get("invocation_dir") or "",
            fixture_roles=kwargs.get("fixture_roles"),
            fixture_kind=kwargs.get("fixture_kind") or "negative",
            body_file=kwargs.get("body_file") or "body.md",
        )
    return outcome
