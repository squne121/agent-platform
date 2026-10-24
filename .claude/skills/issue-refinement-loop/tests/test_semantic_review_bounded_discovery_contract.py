"""Issue #2973 AC1-AC6: path 未列挙 role の bounded discovery 契約の static test。

`issue-design-reviewer.md` と `semantic-design-review.md` の単一 `Consumer-audit` section（と reviewer へ渡す起動
prompt の blockquote）が、required role 単位の bounded discovery を要求していることと、path 未列挙 role を持つ
synthetic fixture の静的 ground truth を固定する。model の発見能力は証明しない（実 SubAgent runtime smoke が担う）。
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[3]
_AGENT_RELATIVE = ".claude/agents/issue-design-reviewer.md"
_AGENT_PATH = _REPO_ROOT / _AGENT_RELATIVE
_REFERENCE_PATH = _REPO_ROOT / ".claude/skills/issue-refinement-loop/references/semantic-design-review.md"
_FIXTURES_DIR = _TESTS_DIR / "fixtures"
_DOCS = {"agent": _AGENT_PATH, "reference": _REFERENCE_PATH}


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


E63 = _load("issue2963_reachability_runtime_evaluator", "issue2963_reachability_runtime_evaluator.py")
EVAL = _load("issue2973_bounded_discovery_runtime_evaluator", "issue2973_bounded_discovery_runtime_evaluator.py")

# #2963 の fixture（本 Issue では変更しない。path の独立性を検証するためだけに参照する）。
_FIXTURES_2963 = ("consumer_reachability_negative_case", "consumer_reachability_positive_control_case")
_FIXTURES_2963 += ("consumer_reachability_simple_docs_only_case",)

# #2961 の live source は fixture / test のどこからも import も読み取りもしない。
# 自己検査で自分自身に一致しないよう、名前は連結で組み立てる。
_LIVE_SOURCE_NAMES = (
    "extension_surface_" + "policy_matcher",
    "check_issue_" + "contract",
    "contract_readiness_" + "check",
)

_OWN_FILES = (
    "test_semantic_review_bounded_discovery_contract.py",
    "test_issue_design_reviewer_bounded_discovery_evaluator.py",
    "issue2973_bounded_discovery_runtime_evaluator.py",
)

# ---------------------------------------------------------------------------
# Markdown helpers（#2963 の static test と同じ判別）
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _headings(text: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    in_fence = False
    for index, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            result.append((index, len(match.group(1)), match.group(2)))
    return result


_AUDIT_TITLE = re.compile(r"consumer[\s_-]*audit", re.IGNORECASE)
_PARALLEL_TITLE = re.compile(
    r"consumer|reachab|inventory|checklist|dataflow|data flow|call[- ]graph|discover|探索|発見", re.IGNORECASE
)


def _audit_section(text: str) -> str:
    headings = _headings(text)
    lines = text.splitlines()
    starts = [(i, level) for i, level, title in headings if _AUDIT_TITLE.search(title)]
    assert len(starts) == 1, f"expected exactly one consumer-audit section, got {len(starts)}"
    start, level = starts[0]
    end = len(lines)
    for i, other_level, _title in headings:
        if i > start and other_level <= level:
            end = i
            break
    return "\n".join(lines[start:end])


def _launch_prompt(text: str) -> str:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            current.append(line.lstrip("> ").rstrip())
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    prompts = ["\n".join(block) for block in blocks if any("bundle.json" in line for line in block)]
    assert len(prompts) == 1, f"expected exactly one launch prompt block, got {len(prompts)}"
    return prompts[0]


def _flat(text: str) -> str:
    """空白を 1 つに畳む。日本語どうしの間の改行は連結する（折り返し位置に依存させない）。"""
    joined = re.sub(r"(?<=[^\x00-\x7f])\s*\n\s*(?=[^\x00-\x7f])", "", text)
    return re.sub(r"\s+", " ", joined)


def _scopes(name: str) -> dict[str, str]:
    """document 名 -> {section, prompt}（正規化済み）。"""
    text = _read(_DOCS[name])
    return {"section": _flat(_audit_section(text)), "prompt": _flat(_launch_prompt(text))}


def _assert_needles(label: str, haystack: str, needles: tuple[str, ...]) -> None:
    for needle in needles:
        assert needle in haystack, f"{label}: missing {needle!r}"


# ---------------------------------------------------------------------------
# AC1-AC4
# ---------------------------------------------------------------------------


def test_reviewer_contract_requires_role_scoped_bounded_discovery_when_paths_unlisted() -> None:
    """AC1: required role 単位の discovery（path を解決できる role は直接 Read、未解決 role だけ 2 lane で発見）。"""
    for name in _DOCS:
        scopes = _scopes(name)
        _assert_needles(
            f"{name} section",
            scopes["section"],
            (
                "required role（producer / parser / evaluator / matcher / decision-critical consumer）単位に適用する",
                "repository 相対 path を一意に記載している role は、その path を直接 Read し、検索しない",
                "explicit path を持つ role を再探索しない",
                "path の記載がない、または一意に解決できない role だけ",
                "named symbol / evaluator / caller 名を query にして",
                "repository root 配下に限定した Grep / Glob（専用 lane）または root 束縛の Bash find / grep",
                "（Bash lane）で候補を発見し",
                "Read で確認してから audit を続ける",
                "lane は session の effective tool pool に従う",
                "frontmatter の `tools` 宣言は effective tool pool を保証しない",
                "専用 Grep / Glob があれば専用 lane、無ければ Bash lane で discovery し",
            ),
        )
        _assert_needles(
            f"{name} launch prompt",
            scopes["prompt"],
            (
                "pinned body が repository 相対 path を列挙していない role",
                "named symbol を query にした bounded discovery",
                "path を列挙している role は検索せず直接読む",
                "explicit path を持つ role を再探索しない",
                "discovery の lane は session で実際に使える tool に従う",
                "専用の Grep / Glob tool が使えるなら、それだけを使い",
                "`path` に `<root>` 配下の絶対 path を明示する",
                "専用の Grep / Glob が使えない（`No such tool available` になる）場合は、",
                "Bash の `find` / `grep` で discovery する",
                "Grep / grep では未解決 role の named symbol を",
                "Glob / find（`-name` / `-iname` / `-path`）では未解決 role の file 名断片を含める",
            ),
        )
    # 起動 prompt は両 document で同一内容のまま。
    assert _flat(_launch_prompt(_read(_AGENT_PATH))) == _flat(_launch_prompt(_read(_REFERENCE_PATH)))
    # #2963 の既存 needle と `観測できなかった path と理由` の 1 回出現は保つ。
    for name, path in _DOCS.items():
        scopes = _scopes(name)
        _assert_needles(
            f"{name} #2963 prompt needles",
            scopes["prompt"],
            ("pinned body が列挙する", "4 役すべての各 file を", "evaluator / matcher の file"),
        )
        _assert_needles(f"{name} #2963 section needles", scopes["section"], ("4 役すべての各 file を",))
        assert _flat(_read(path)).count("観測できなかった path と理由") == 1, name


_FORBIDDEN_BASH_EXAMPLES = (
    "rg",
    "ugrep",
    "git grep",
    "git ls-files",
    "ls -R",
    "env grep",
    "timeout 5 grep",
    "cd <root> && grep",
    "xargs grep",
    "cat",
    "sed",
)


def _backticked(segment: str) -> list[str]:
    return re.findall(r"`([^`]+)`", segment)


def test_prompt_allowlist_wording_matches_evaluator_shape_one_to_one() -> None:
    """起動 prompt の Bash allowlist 文言と evaluator の narrowly supported shape が一対一に対応する（AC1 / AC2）。"""
    for name in _DOCS:
        prompt = _scopes(name)["prompt"]
        section = _scopes(name)["section"]
        # ONLY Bash: exact root / exact HEAD / eligible find / grep だけ。それ以外はすべて契約違反。
        _assert_needles(
            f"{name} prompt",
            prompt,
            (
                "reviewer 区間で許される Bash は、(1) の root 解決 command、(2) の HEAD 解決 command、"
                "eligible な find / grep の 3 種類だけである",
                "これ以外の Bash",
                "はすべて契約違反である",
                "単一の simple command",
                "`<root>` 配下の絶対 path を明示する（相対 path・path の省略・`..`・root 外は禁止）",
                "`--include X` の分離形式は禁止",
                "`-l` と `--include=` / `--exclude-dir=` を併用することを推奨する",
            ),
        )
        for example in _FORBIDDEN_BASH_EXAMPLES:
            assert example in prompt, f"{name} prompt: forbidden Bash example {example!r} is not listed"
            assert example in section, f"{name} section: forbidden Bash example {example!r} is not listed"
        grep_segment = prompt.split("grep は ", 1)[1].split("だけを使い", 1)[0]
        grep_tokens = _backticked(grep_segment)
        short = {token[1:] for token in grep_tokens if re.fullmatch(r"-[A-Za-z]", token)}
        long_prefixes = {token.split("X")[0] for token in grep_tokens if token.startswith("--")}
        assert short == set(EVAL.GREP_SHORT_FLAGS), f"{name}: grep flags in the prompt differ from the evaluator"
        assert long_prefixes == set(EVAL.GREP_LONG_FLAG_PREFIXES)
        find_segment = prompt.split("find は ", 1)[1].split("だけを使う", 1)[0]
        assert set(_backticked(find_segment)) == {*EVAL.FIND_VALUE_PRIMARIES, *EVAL.FIND_FLAG_PRIMARIES}
        # 同じ flag 集合を section 側でも列挙している。
        section_grep = section.split("grep が使える flag は ", 1)[1].split(" だけ", 1)[0]
        section_short = {t[1:] for t in _backticked(section_grep) if re.fullmatch(r"-[A-Za-z]", t)}
        assert section_short == set(EVAL.GREP_SHORT_FLAGS)
    # prompt が許可する shape を evaluator が実際に受理し、禁止例は違反として扱う（文言だけの一致で終わらせない）。
    root = "/synthetic/root"
    eligible = (
        f"grep -rln --include='*.py' SymA {root}",
        f"grep -rn -E 'SymA|SymB' {root}/dir",
        f"grep -rne SymA {root}",
        f"find {root} -type f -name 'frag*' -o -iname 'FRAG*'",
        f"find {root}/d -maxdepth 3 -path '*/frag*'",
    )
    for command in eligible:
        parsed = EVAL.parse_bash_search(command, root)
        assert parsed["reason"] is None and parsed["scope_violation"] is None, command
    forbidden = (
        f"rg -n SymA {root}",
        "git grep SymA",
        "git ls-files",
        f"ls -R {root}",
        f"env grep SymA {root}",
        f"timeout 5 grep SymA {root}",
        f"cd {root} && grep SymA {root}",
        f"xargs grep SymA {root}",
        f"cat {root}/x.py",
        f"sed -n 1,5p {root}/x.py",
        f"grep SymA {root}; ls",
        f"grep SymA {root}|cat",
        f"grep --include '*.py' SymA {root}",
        f"find {root} -name x -exec cat {{}} +",
    )
    for command in forbidden:
        assert EVAL.parse_bash_search(command, root)["reason"] is not None, command


def test_discovery_bounds_match_evaluator_constants_and_no_new_analyzer_or_registry() -> None:
    """AC2: 文書中の bound の数値が evaluator 定数と同一（3 箇所: 文書 / evaluator / 本 test）で、新機構を足さない。"""
    # Issue 本文「固定する bound の値」（実装側の都合で変更しない）。
    assert (EVAL.DISCOVERY_SEARCH_CALL_MAX, EVAL.DISCOVERY_SOURCE_READ_MAX) == (8, 8)
    assert EVAL.SEARCH_SCOPE == "repository_root_only" and EVAL.DISCOVERY_TOOLS == ("Grep", "Glob")
    assert EVAL.DISCOVERY_BASH_LANE == ("find", "grep")
    for name in _DOCS:
        scopes = _scopes(name)
        for scope, haystack in scopes.items():
            search = re.findall(r"DISCOVERY_SEARCH_CALL_MAX: (\d+)", haystack)
            reads = re.findall(r"DISCOVERY_SOURCE_READ_MAX: (\d+)", haystack)
            assert search == [str(EVAL.DISCOVERY_SEARCH_CALL_MAX)], f"{name} {scope}: {search}"
            assert reads == [str(EVAL.DISCOVERY_SOURCE_READ_MAX)], f"{name} {scope}: {reads}"
        _assert_needles(
            f"{name} section",
            scopes["section"],
            (
                "`DISCOVERY_SEARCH_CALL_MAX: 8`",
                "`DISCOVERY_SOURCE_READ_MAX: 8`",
                "`SEARCH_SCOPE: repository_root_only`",
                "`DISCOVERY_TOOLS: [Grep, Glob]`",
                "`DISCOVERY_BASH_LANE: [find, grep]`",
                "専用 Grep / Glob の tool_use 1 件、または eligible な Bash find / grep 1 件を 1 search call と数え",
                "成功・失敗を問わず数える",
                "専用 lane と Bash lane の混在は合算する",
                "`bundle.json` と `body_file` を除く repository file の Read の合計",
                "`path` の省略・相対 path・root 外 path・`..` による脱出・root 外を指す絶対 pattern は違反",
                "任意の Bash は discovery として数えない",
                "それ以外の Bash",
                "search call として数えたうえで違反とする",
                "root / HEAD の解決 command は non-discovery であり search bound に算入しない",
                "新しい analyzer / generic shell parser / schema / registry / approval layer は追加しない",
            ),
        )
        _assert_needles(
            f"{name} prompt",
            scopes["prompt"],
            (
                "専用 Grep / Glob と eligible な Bash find / grep の合計 8 回以内",
                "`bundle.json` と `body_file` 以外の Read は 8 回以内",
            ),
        )
    # evaluator は #2963 の scanner / helper を import して再利用する（複製しない）。
    for helper in (
        "scan_tool_records",
        "_high_ref_union",
        "_is_root_command",
        "_is_head_command",
        "_read_target",
        "iter_stream_events",
        "parse_raw_result_object",
        "load_runner_module",
    ):
        assert getattr(EVAL, helper) is getattr(E63, helper), f"{helper} must be the reused #2963 helper"
    # 汎用 analyzer / registry / runner を新設しない（新 evaluator は class を定義しない）。
    tree = ast.parse((_TESTS_DIR / "issue2973_bounded_discovery_runtime_evaluator.py").read_text(encoding="utf-8"))
    assert [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)] == []


def test_unlisted_path_alone_is_not_high_and_unobservable_after_discovery_is_not_clear() -> None:
    """AC3: path 未記載 / 専用 tool 不在それ自体は high にしない / 因果 / decoy / 発見できない場合に限り high 以上。"""
    for name in _DOCS:
        scopes = _scopes(name)
        _assert_needles(
            f"{name} section",
            scopes["section"],
            (
                "path が未記載であること自体、および専用 Grep / Glob が session tool pool に無いこと自体を",
                "理由に high にしない",
                "Bash lane で続行する",
                "成功した（error ではない）検索結果に target source の path が現れてから、その file を Read する",
                "grep では hit した file の path、Glob / find では結果の path 行として現れることを要する",
                "error の eligible call は bound に算入されるが discovery の根拠には使わない",
                "body・test・evaluator 内の自己参照 literal hit だけでは discovery 成功としない",
                "同名 symbol を持つ decoy があり得るため、最初の hit を盲目的に Read せず",
                "decision-critical consumer の import / call-site から decoy ではない target を確定する",
                "bounded discovery（いずれの lane でも）を尽くしても必要な source を発見・観測できなかった場合に限り",
                "`assessment: clear` にせず",
                "観測できなかった symbol と試行した検索を `evidence_refs` に残して high 以上の finding にする",
                "「観測不能は clear にしない」と整合する",
            ),
        )
        _assert_needles(
            f"{name} prompt",
            scopes["prompt"],
            (
                "専用 Grep / Glob が無いこと自体を理由に high にしない",
                "path が未記載であること自体を理由に high にしない",
                "成功した検索結果に target source の path が現れてから、その file を Read する",
                "最初の hit を盲目的に Read せず",
                "consumer の import / call-site から target を確定する",
                "bounded discovery を尽くしても必要な source を発見・観測できなかった場合に限り",
                "観測できなかった symbol と試行した検索を `evidence_refs` に残して high 以上の finding にする",
            ),
        )
        # 順序: 「発見・観測できなかった場合に限り」の後に「観測できなかった symbol と試行した検索」が来る。
        for scope, haystack in scopes.items():
            marker = "発見・観測できなかった場合に限り"
            first = haystack.index(marker)
            assert haystack.index("観測できなかった symbol と試行した検索", first) > first, f"{name} {scope}"
        # 既存の義務文言（観測できなかった path と理由）は別の文として 1 回だけ残り、置換されていない。
        assert _flat(_read(_DOCS[name])).count("観測できなかった path と理由") == 1
        for scope, haystack in scopes.items():  # section と launch prompt に 1 回ずつ
            assert haystack.count("観測できなかった symbol と試行した検索") == 1, f"{name} {scope}"


def test_single_audit_section_and_authority_separation_preserved() -> None:
    """AC4: 単一 Consumer-audit section に収まり、権限分離と #2828 の責務を侵さず、docs-only に一律要求しない。"""
    for name, path in _DOCS.items():
        text = _read(path)
        headings = _headings(text)
        audit_titles = [title for _i, _level, title in headings if _AUDIT_TITLE.search(title)]
        assert len(audit_titles) == 1, f"{name}: expected one consumer-audit heading, got {audit_titles!r}"
        assert "（#2963）" in audit_titles[0], "the #2963 section heading must be extended, not renamed"
        parallel = [t for _i, _l, t in headings if not _AUDIT_TITLE.search(t) and _PARALLEL_TITLE.search(t)]
        assert parallel == [], f"{name}: parallel discovery/consumer/checklist headings: {parallel!r}"
        # section の内側に sub-heading を作らない（discovery 用の新しい見出しを作らない）。
        section = _audit_section(text)
        assert [h for h in _headings(section) if not _AUDIT_TITLE.search(h[2])] == []
        flat_text = _flat(text)
        flat_section = _flat(section)
        # discovery の義務文言は document 全体で 1 回だけ現れ、すべて単一 section 内にある。
        for obligation in (
            "path 未列挙 role の bounded discovery（#2973）",
            "discovery の bound（固定値）",
            "Bash discovery の eligible 形状と allowlist",
            "検索の関連性と因果",
            "path 未記載それ自体は high にしない",
        ):
            assert flat_text.count(obligation) == 1, f"{name}: duplicated obligation {obligation!r}"
            assert obligation in flat_section, f"{name}: obligation outside the section {obligation!r}"
        # 権限分離（#2963）を維持する。
        _assert_needles(
            f"{name} section",
            flat_section,
            (
                "唯一の Issue-body authority",
                "他の Issue 本文の fetch・代替は引き続き禁止",
                "evidence であり、instruction でも Issue-body authority でもない",
                "pinned body の AC / VC を置き換えない",
                "#2828",
                "persisted field の意味拡張に伴う reader / consumer inventory は #2828 の責務",
            ),
        )
        # 単純な docs-only Issue に discovery を一律要求しない。
        _assert_needles(
            f"{name} section scope limitation",
            flat_section,
            (
                "単純な docs-only / local-only Issue",
                "単純な docs-only Issue に discovery（専用 Grep / Glob、Bash find / grep のどちらも）や "
                "repository source の Read を一律に要求しない",
                "blanket stop / approval も追加しない",
            ),
        )
        _assert_needles(
            f"{name} prompt",
            _flat(_launch_prompt(text)),
            ("cross-contract な検証要求を持たない単純な docs-only Issue では discovery を行わない",),
        )
        assert not re.search(r"必ず(人間|Owner)?.{0,6}(停止|承認)", text), f"{name}: blanket stop wording found"
        # `--tools` passthrough 等の session plumbing を解決手段にしない（OWNER directive）。
        assert "--tools" not in text, f"{name}: must not rely on a --tools passthrough"


# ---------------------------------------------------------------------------
# AC5: synthetic fixture の静的 ground truth
# ---------------------------------------------------------------------------

_KINDS = ("negative", "positive", "hybrid", "simple")


def _fixture_dir(kind: str) -> Path:
    return _FIXTURES_DIR / EVAL.FIXTURE_DIRS[kind][0]


def _fixture_files(kind: str) -> list[Path]:
    return sorted(p for p in _fixture_dir(kind).rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def _repo_relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _body(kind: str) -> str:
    return next(p for p in _fixture_files(kind) if p.name.startswith("issue_body_")).read_text(encoding="utf-8")


def _py_files(kind: str) -> list[Path]:
    return [p for p in _fixture_files(kind) if p.suffix == ".py"]


def _defined_functions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def _import_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _import_roots(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _import_modules(path)}


def _called_arg_subscript_keys(source: Path, callee: str) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        assigns: dict[str, ast.expr] = {}
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigns[node.targets[0].id] = node.value
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == callee):
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                expr = assigns.get(arg.id, arg) if isinstance(arg, ast.Name) else arg
                for sub in ast.walk(expr):
                    if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
                        keys.add(str(sub.slice.value))
    return keys


def _evaluator_parameters(path: Path, symbol: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == symbol)
    return [arg.arg for arg in func.args.args]


def _load_decide(kind: str) -> Any:
    """fixture consumer を unique module 名で読み込む（#2963 と同じ prefix-unique 方式）。

    fixture 内の sibling module は `<prefix>_` 接頭辞の bare 名で import される。統合 pytest session で同名の別
    module と sys.modules 上で衝突しないよう、読み込み前に同接頭辞の既存 entry を退避し、終了後に接頭辞一致の
    entry を全て除去して元へ戻す。"""
    directory = str(_fixture_dir(kind))
    prefix = EVAL.FIXTURE_DIRS[kind][1]
    roles = EVAL.fixture_roles(kind)
    unique = f"issue2973_fixture_{prefix}_consumer"
    stashed = {name: mod for name, mod in sys.modules.items() if name.startswith(f"{prefix}_") or name == unique}
    for name in stashed:
        sys.modules.pop(name, None)
    sys.path.insert(0, directory)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # fixture directory に __pycache__ を作らない
    try:
        spec = importlib.util.spec_from_file_location(unique, _REPO_ROOT / roles["consumer"]["path"])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
        return getattr(module, roles["consumer"]["symbol"])
    finally:
        sys.dont_write_bytecode = previous
        sys.path.remove(directory)
        for name in [m for m in sys.modules if m.startswith(f"{prefix}_") or m == unique]:
            sys.modules.pop(name, None)
        sys.modules.update(stashed)


_BODY_WITHOUT_GIT_DIFF = "# AC1\n$ uv run --locked pytest tests/test_x.py\n"
_BODY_WITH_GIT_DIFF = "# AC1\n$ git diff --exit-code origin/main -- a.py\n"


def test_unlisted_path_fixture_ground_truth_and_independence() -> None:
    all_roles = {kind: EVAL.fixture_roles(kind) for kind in _KINDS}

    # ground truth metadata は実在する fixture file と一致する。
    for kind in ("negative", "positive", "hybrid"):
        roles = all_roles[kind]
        assert list(roles) == ["producer", "parser", "evaluator", "consumer"]
        for role, info in roles.items():
            assert set(info) == {"path", "symbol", "file_fragment", "listed", "decoy_path"}
            assert (_REPO_ROOT / info["path"]).is_file(), f"{kind}/{role}: target source is missing"
            assert Path(info["path"]).stem == info["file_fragment"]
        listed = [role for role, info in roles.items() if info["listed"]]
        assert listed == (["producer", "parser"] if kind == "hybrid" else []), f"{kind}: listed={listed}"
        decoys = {role: info["decoy_path"] for role, info in roles.items() if info["decoy_path"]}
        if kind == "negative":
            assert list(decoys) == ["evaluator"] and (_REPO_ROOT / decoys["evaluator"]).is_file()
        else:
            assert decoys == {}, f"{kind}: only the negative fixture has a decoy"
    assert all_roles["simple"] == {}
    allowed = EVAL.fixture_allowed_read_paths("simple")
    assert len(allowed) == 1 and (_REPO_ROOT / allowed[0]).is_file()
    assert EVAL.fixture_allowed_read_paths("negative") == []

    # (1) body: negative / positive は named symbol のみ。hybrid は producer / parser の path のみ。
    for kind in ("negative", "positive", "hybrid"):
        body = _body(kind)
        for role, info in all_roles[kind].items():
            name = Path(info["path"]).name
            if info["listed"]:
                assert info["path"] in body, f"{kind}/{role}: listed path missing"
            else:
                assert info["symbol"] in body and info["file_fragment"] in body, f"{kind}/{role}: symbol missing"
                assert info["path"] not in body and f"{name}" not in body, f"{kind}/{role}: unlisted path leaked"
                assert _fixture_dir(kind).name not in body or kind == "hybrid", f"{kind}: fixture directory leaked"
        if kind == "negative":
            assert "decoy" not in body and all_roles[kind]["evaluator"]["decoy_path"] not in body
    hybrid_unlisted = [i for i in all_roles["hybrid"].values() if not i["listed"]]
    assert len(hybrid_unlisted) == 2
    for info in hybrid_unlisted:
        assert _fixture_dir("hybrid").name not in info["symbol"] and info["path"] not in _body("hybrid")

    # (2) simple: cross-contract な検証要求を持たない（role 名・symbol・source path を持たず、allowed path だけ）。
    simple = _body("simple")
    assert allowed[0] in simple
    for kind in ("negative", "positive", "hybrid"):
        for info in all_roles[kind].values():
            assert info["symbol"] not in simple and info["file_fragment"] not in simple
    for token in ("evaluator", "consumer", "producer", "parser", "matcher", "cross-contract"):
        assert token not in simple, f"simple body must not carry cross-contract wording: {token}"
    assert not [p for p in _fixture_files("simple") if p.suffix == ".py"]

    # (3) 4 fixture の path は一意で、他 fixture / #2963 の fixture の path の部分文字列にならない。
    path_sets = {
        kind: [_repo_relative(_fixture_dir(kind)), *(_repo_relative(p) for p in _fixture_files(kind))]
        for kind in _KINDS
    }
    for left, right in itertools.permutations(_KINDS, 2):
        for a in path_sets[left]:
            for b in path_sets[right]:
                assert a not in b, f"fixture path {a!r} ({left}) is a substring of {b!r} ({right})"
    old_paths = [
        _repo_relative(p)
        for directory in _FIXTURES_2963
        for p in [_FIXTURES_DIR / directory, *(q for q in (_FIXTURES_DIR / directory).rglob("*") if q.is_file())]
        if "__pycache__" not in p.parts
    ]
    assert old_paths, "the #2963 fixtures must exist"
    for kind in _KINDS:
        for a in path_sets[kind]:
            for b in old_paths:
                assert a not in b and b not in a, f"{a!r} ({kind}) collides with the #2963 fixture path {b!r}"
    names = [p.name for kind in _KINDS for p in _fixture_files(kind)]
    assert len(names) == len(set(names)), "fixture file names must be unique across fixtures"

    # (4) named symbol は当該 fixture の source 側で一意に定義される（negative の evaluator だけ decoy と同名 2 件）。
    definitions: dict[str, dict[str, list[str]]] = {}
    for kind in ("negative", "positive", "hybrid"):
        defs: dict[str, list[str]] = {}
        for path in _py_files(kind):
            for func in _defined_functions(path):
                defs.setdefault(func, []).append(_repo_relative(path))
        definitions[kind] = defs
        for role, info in all_roles[kind].items():
            sites = defs.get(info["symbol"], [])
            expected = [info["path"]] if not info["decoy_path"] else sorted([info["path"], info["decoy_path"]])
            assert sorted(sites) == expected, f"{kind}/{role}: {info['symbol']} defined in {sites}"
    for left, right in itertools.permutations(("negative", "positive", "hybrid"), 2):
        for info in all_roles[left].values():
            assert info["symbol"] not in definitions[right], (
                f"symbol {info['symbol']} of {left} also defined in {right}"
            )
            sources = "".join(p.read_text(encoding="utf-8") for p in _py_files(right))
            assert info["symbol"] not in sources and info["file_fragment"] not in sources
    old_sources = "".join(
        p.read_text(encoding="utf-8") for d in _FIXTURES_2963 for p in (_FIXTURES_DIR / d).rglob("*.py")
    )
    for kind in ("negative", "positive", "hybrid"):
        for info in all_roles[kind].values():
            assert info["symbol"] not in old_sources and info["file_fragment"] not in old_sources
    # negative: consumer が import する側が target で、decoy は import されない。
    neg = all_roles["negative"]
    consumer_imports = _import_modules(_REPO_ROOT / neg["consumer"]["path"])
    assert neg["evaluator"]["file_fragment"] in consumer_imports
    decoy_stem = Path(neg["evaluator"]["decoy_path"]).stem
    assert decoy_stem != neg["evaluator"]["file_fragment"]
    assert decoy_stem not in neg["evaluator"]["file_fragment"] and neg["evaluator"]["file_fragment"] not in decoy_stem
    for path in _py_files("negative"):
        assert decoy_stem not in _import_modules(path), f"{path.name} imports the decoy"
    # 全 fixture: 4 役の module 名は互いに他の module 名の部分文字列にならない（import 由来の帰属が曖昧にならない）。
    stems = [Path(i["path"]).stem for k in ("negative", "positive", "hybrid") for i in all_roles[k].values()]
    stems.append(decoy_stem)
    for a, b in itertools.permutations(stems, 2):
        assert a not in b, f"module stem {a!r} is a substring of {b!r}"

    # dataflow gap: negative / hybrid は VC command 本文が consumer から evaluator へ届かず、positive は届く。
    for kind in ("negative", "hybrid"):
        roles = all_roles[kind]
        assert _evaluator_parameters(_REPO_ROOT / roles["evaluator"]["path"], roles["evaluator"]["symbol"]) == [
            "ac_vc_refs"
        ]
        keys = _called_arg_subscript_keys(_REPO_ROOT / roles["consumer"]["path"], roles["evaluator"]["symbol"])
        assert "ac" in keys and "command" not in keys
    pos = all_roles["positive"]
    assert _evaluator_parameters(_REPO_ROOT / pos["evaluator"]["path"], pos["evaluator"]["symbol"]) == [
        "ac_vc_commands"
    ]
    pos_keys = _called_arg_subscript_keys(_REPO_ROOT / pos["consumer"]["path"], pos["evaluator"]["symbol"])
    assert {"ac", "command"} <= pos_keys
    decide = {kind: _load_decide(kind) for kind in ("negative", "positive", "hybrid")}
    assert decide["negative"](_BODY_WITHOUT_GIT_DIFF, {"AC1"})["status"] == "pass"  # evidence が届かず見逃す
    assert decide["hybrid"](_BODY_WITHOUT_GIT_DIFF, {"AC1"})["status"] == "pass"
    assert decide["positive"](_BODY_WITHOUT_GIT_DIFF, {"AC1"})["status"] == "fail"
    assert decide["positive"](_BODY_WITH_GIT_DIFF, {"AC1"})["status"] == "pass"
    # decoy は command 本文を受け取る（誤って clear と読ませる罠）。target は受け取らない。
    decoy_text = (_REPO_ROOT / neg["evaluator"]["decoy_path"]).read_text(encoding="utf-8")
    assert (
        "ac_vc_commands" in decoy_text and "ac_vc_commands" not in (_REPO_ROOT / neg["evaluator"]["path"]).read_text()
    )

    # test / evaluator 内の symbol literal は自己参照 hit であり、target source の代替として数えない。
    self_files = [Path(__file__), _TESTS_DIR / "issue2973_bounded_discovery_runtime_evaluator.py"]
    for kind in ("negative", "hybrid"):
        for role, info in all_roles[kind].items():
            hits = "\n".join(
                f"{_repo_relative(path)}:{number}:    value = {literal!r}"
                for path in self_files
                for number, literal in enumerate((info["symbol"], info["path"]), 1)
            )
            assert not EVAL.result_lists_path(hits, str(_REPO_ROOT), info["path"]), (
                f"{kind}/{role}: a self-referential literal hit must not count as discovery success"
            )
            assert EVAL.result_lists_path(f"{info['path']}\n{hits}", str(_REPO_ROOT), info["path"])

    # (5) fixture と test は #2961 の live source を import も読み取りもしない。
    own = [_TESTS_DIR / name for name in _OWN_FILES]
    for path in [*own, *(p for kind in _KINDS for p in _fixture_files(kind))]:
        assert path.is_file(), f"missing file: {path}"
        content = path.read_text(encoding="utf-8")
        for name in _LIVE_SOURCE_NAMES:
            assert name not in content, f"{path.name} references live source {name}"
        if path.suffix == ".py":
            assert not (_import_roots(path) & set(_LIVE_SOURCE_NAMES)), f"{path.name} imports a live source"


# ---------------------------------------------------------------------------
# AC6: frontmatter / 対象外 file の不変
# ---------------------------------------------------------------------------


def _frontmatter_block(text: str) -> str:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "agent definition must start with a frontmatter block"
    return match.group(1)


_AGENT_FRONTMATTER_KEYS = {"name", "description", "tools", "disallowedTools", "model", "effort", "permissionMode"}
_AGENT_TOOLS = ["Bash", "Read", "Grep", "Glob"]
_AGENT_DISALLOWED_TOOLS = ["Edit", "Write", "MultiEdit", "Agent", "Skill"]
_SKILL_DIR = _REPO_ROOT / ".claude/skills/issue-refinement-loop"


def _agent_contract_violations(text: str, *, file_stem: str) -> list[str]:
    """`issue-design-reviewer` 定義の構造契約違反を列挙する（空 list = 契約どおり）。

    移管元の `*_unchanged_from_origin_main` は、PR 中は必ず差分を検出し、merge 後は自己比較で常に真になる
    (vacuous) ため、frontmatter の必須 key・tool 制約・Agent 名・参照先 file の存在を直接検査する。"""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return ["no frontmatter block"]
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return ["frontmatter is not valid YAML"]
    if not isinstance(front, dict):
        return ["frontmatter is not a mapping"]
    found: list[str] = []
    if set(front) != _AGENT_FRONTMATTER_KEYS:
        found.append(f"frontmatter keys differ: {sorted(set(front) ^ _AGENT_FRONTMATTER_KEYS)}")
    if front.get("name") != file_stem or front.get("name") != "issue-design-reviewer":
        found.append("name must equal the file stem `issue-design-reviewer`")
    if not (isinstance(front.get("description"), str) and front["description"].strip()):
        found.append("description must be a non-empty string")
    if front.get("tools") != _AGENT_TOOLS:
        found.append("tools must be exactly the read-only set Bash/Read/Grep/Glob")
    if front.get("disallowedTools") != _AGENT_DISALLOWED_TOOLS:
        found.append("disallowedTools must forbid Edit/Write/MultiEdit/Agent/Skill")
    if set(front.get("tools") or []) & set(front.get("disallowedTools") or []):
        found.append("tools and disallowedTools overlap")
    if front.get("permissionMode") != "dontAsk":
        found.append("permissionMode must be dontAsk")
    if front.get("model") != "sonnet" or front.get("effort") != "high":
        found.append("model/effort must stay sonnet/high")
    return found


def test_reviewer_agent_definition_contract_is_enforced_not_self_compared() -> None:
    """AC6 (置換): 実 file が契約を満たし、契約を破る変異は必ず検出される（自己比較ではない）。"""
    text = _read(_AGENT_PATH)
    assert _AGENT_PATH.is_file() and _AGENT_PATH.stem == "issue-design-reviewer"
    assert _agent_contract_violations(text, file_stem=_AGENT_PATH.stem) == []
    # 対照: body の本 Issue 固有記述が残っている（定義が空の殻でないこと）。
    assert "path 未列挙 role の bounded discovery（#2973）" in text

    stem = _AGENT_PATH.stem
    mutations = {
        "write tool granted": text.replace("  - Grep\n", "  - Grep\n  - Write\n", 1),
        "edit not disallowed": text.replace("  - Edit\n", "", 1),
        "permission mode relaxed": text.replace("permissionMode: dontAsk", "permissionMode: acceptEdits", 1),
        "model changed": text.replace("model: sonnet", "model: opus", 1),
        "renamed": text.replace("name: issue-design-reviewer", "name: issue-reviewer", 1),
        "no frontmatter": text.split("\n---\n", 1)[1],
    }
    for label, mutated in mutations.items():
        assert mutated != text, f"mutation did not change the text: {label}"
        assert _agent_contract_violations(mutated, file_stem=stem), f"undetected: {label}"
    assert _agent_contract_violations(text, file_stem="issue-reviewer"), "file stem mismatch undetected"


def test_reviewer_agent_references_resolve_to_migrated_files() -> None:
    """AC6 (置換): Agent 定義・reference が名指す Skill 配下 script / reference / schema が実在する。"""
    agent_text, reference_text = _read(_AGENT_PATH), _read(_REFERENCE_PATH)
    for text in (agent_text, reference_text):
        for schema in set(re.findall(r"schemas/[A-Za-z0-9_.-]+\.json", text)):
            assert (_REPO_ROOT / schema).is_file(), f"dangling schema reference: {schema}"
    for script in set(re.findall(r"\b([a-z_]+\.py)\b", agent_text)) & {
        "semantic_review_transport.py",
        "semantic_review_trigger.py",
        "join_review_results.py",
    }:
        assert (_SKILL_DIR / "scripts" / script).is_file(), f"dangling script reference: {script}"
    assert "semantic_review_transport.py" in agent_text and "semantic-design-review.md" in agent_text
    assert (_SKILL_DIR / "references/semantic-design-review.md").is_file()
    # reference は同じ Agent 名を起動対象として名指し、Agent 定義 file を正本として参照する。
    assert "issue-design-reviewer" in reference_text and _AGENT_RELATIVE in reference_text


def test_prompt_and_section_require_bash_lane_start_and_role_specific_query_patterns() -> None:
    """fix_delta iteration 2: 専用 tool 不在時は Bash lane へ直行する。

    query pattern は未解決 role の body 記載文字列に限り、fixture / Issue 共通 prefix だけの pattern は違反で
    budget を消費する。prompt の文言と evaluator の relevance 規則は一対一に対応する。"""
    for name in _DOCS:
        scopes = _scopes(name)
        _assert_needles(
            f"{name} prompt",
            scopes["prompt"],
            (
                "専用の Grep / Glob の呼び出しが 1 回でも `No such tool available` になったら、",
                "以後は専用 tool を再試行せず",
                "Bash lane だけで discovery する",
                "失敗した専用 tool の呼び出しも search call として数えられる",
                "`grep -rl --include=<glob> <named symbol> <root 配下の絶対 path>` から始めることを推奨する",
                "pinned body に書かれた文字列を verbatim で使い、推測した名前を使わない",
                "grep の pattern は未解決 role の named symbol",
                "Glob / find の name / path pattern は body が挙げる file 名断片",
                "複数 role・fixture・Issue に共通する prefix だけの pattern（例 `*<共通 prefix>*`）",
                "関連しない検索であり、契約違反になるうえ 8 回の search budget も消費する",
                "Bash の `find` / `grep` で discovery する",
                "reviewer 区間で許される Bash は、(1) の root 解決 command、(2) の HEAD 解決 command、"
                "eligible な find / grep の 3 種類だけである",
            ),
        )
        _assert_needles(
            f"{name} section",
            scopes["section"],
            (
                "専用 Grep / Glob が 1 回でも `No such tool available` になったら以後は専用 tool を再試行せず",
                "`grep -rl --include=<glob> <named symbol> <root 配下の絶対 path>` から始まる Bash lane だけを使う",
                "失敗した専用 tool の呼び出しも search call として数えられる",
                "pinned body に書かれた文字列を verbatim で使う",
                "複数 role・fixture・Issue に共通する prefix だけの pattern（例 `*<共通 prefix>*`）",
                "どの未解決 role の file 名断片でもないため関連しない検索であり、8 回の search budget を消費する",
                "固定値",  # bound の固定値節は残る
            ),
        )
        # 未解決 role を 1 件ずつ漏れなく検索で解決し、path を推測して Read しない（consumer も例外ではない）。
        _assert_needles(
            f"{name} prompt per-role resolution",
            scopes["prompt"],
            (
                "未解決 role は 1 件ずつ漏れなく解決する",
                "decision-critical consumer も例外ではなく",
                "consumer の named symbol も検索対象に含める",
                "他 role の hit・同じ directory・命名規則から path を推測して",
                "Read しない（path の推測 Read は禁止。ただし既に Read 済みの別 required-role target source の"
                " import / call-site から target が一意に確定した場合は、"
                "帰属規則 (b) として search を省略して Read してよい。"
                "同じ directory・命名規則・共有 prefix・推測した path だけを根拠にした Read は契約違反）",
                "search path operand は literal resolved-root path。shell expansion で構築しない",
                "unquoted の shell メタ文字",
                "backslash + 改行の行継続は使わない",
                "pattern・`-e` の値・`-name` / `-path` の glob・`--include=` の glob は quote する",
                "quote した alternation で 1 回にまとめてよい",
                "`grep -rlE --include=<glob> '<symbol A>|<symbol B>' <root 配下の絶対 path>`",
            ),
        )
        _assert_needles(
            f"{name} section per-role resolution",
            scopes["section"],
            (
                "未解決 role は 1 件ずつ漏れなく解決し",
                "consumer の path が未記載ならその named symbol も検索対象に含める",
                "他 role の hit・同じ directory・命名規則から",
                "path を推測して Read しない（path の推測 Read は禁止。"
                "ただし既に Read 済みの別 required-role target source の"
                " import / call-site から target が一意に確定した場合は、"
                "帰属規則 (b) として search を省略して Read してよい。"
                "同じ directory・命名規則・共有 prefix・推測した path だけを根拠にした Read は違反）",
                "search path operand は literal resolved-root path であり、shell expansion で構築しない",
                "unquoted の shell メタ文字",
                "backslash + 改行の行継続",
                "`--include=` の glob は quote する",
                "quote した alternation で 1 回にまとめてよい",
            ),
        )
        # 新しい heading は追加しない（既存の単一 Consumer-audit section の拡張のみ）。
        assert _flat(_read(_DOCS[name])).count("観測できなかった path と理由") == 1, name
    # 推奨例の command は eligible 形状として evaluator が実際に受理し、固定 bound は 8 / 8 のまま。
    root = "/synthetic/root"
    parsed = EVAL.parse_bash_search(f"grep -rl --include='*.py' SymA {root}", root)
    assert parsed["reason"] is None and parsed["scope_violation"] is None and parsed["cmd"] == "grep"
    alternation = EVAL.parse_bash_search(f"grep -rlE --include='*.py' 'SymA|SymB' {root}", root)
    assert alternation["reason"] is None and alternation["scope_violation"] is None
    assert (EVAL.DISCOVERY_SEARCH_CALL_MAX, EVAL.DISCOVERY_SOURCE_READ_MAX) == (8, 8)
    # path operand は expansion-free な literal（expansion を含む operand は evaluator が scope 違反にする）。
    expanded = EVAL.parse_bash_search(f"grep -rl SymA {root}/${{HOME:0:0}}../${{HOME:0:0}}../etc", root)
    assert expanded["reason"] == "shell_expansion"
    # unquoted の shell メタ文字（pattern / glob 位置を含む）と行継続は不適格、quote した pattern / glob は適格。
    for hazardous, reason in (
        (f"grep -rl {{SymA,/synthetic/outside}} {root}/src", "shell_expansion"),
        (f"grep -rl SymA$IFS/synthetic/outside {root}/src", "shell_expansion"),
        (f"grep -rl SymA {root}/src/.\\\n./outside", "line_continuation"),
    ):
        assert EVAL.parse_bash_search(hazardous, root)["reason"] == reason, hazardous
    quoted = EVAL.parse_bash_search(f"grep -rl -e 'SymA' --include='*.py' {root}/src", root)
    assert quoted["reason"] is None and quoted["scope_violation"] is None
    # 旧い狭い文言（import 導出 Read を禁じる）が prompt / section に残っていない。
    for name in _DOCS:
        assert "その role を検索で解決していない Read は" not in _flat(_read(_DOCS[name])), name
    # relevance 規則の evaluator 側: 全 role の symbol / file 名断片だけが関連する（fixture 共通 prefix は無関係）。
    for kind in ("negative", "positive"):
        prefix = EVAL.FIXTURE_DIRS[kind][1]
        for role in EVAL.fixture_roles(kind).values():
            assert not EVAL._query_matches_role({"kind": "fragment", "texts": [f"**/*{prefix}*"]}, role)
            assert EVAL._query_matches_role({"kind": "fragment", "texts": [f"**/{role['file_fragment']}*"]}, role)
