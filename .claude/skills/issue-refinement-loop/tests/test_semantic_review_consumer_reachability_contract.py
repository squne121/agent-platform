"""Issue #2963 AC1-AC6: semantic design review の consumer input reachability 契約の static test。

`issue-design-reviewer.md` と `semantic-design-review.md` が、検証要求の consumer input reachability
audit を単一の section として要求していることと、self-contained な synthetic fixture の静的 ground truth を
固定する。model の発見能力は証明しない（実 SubAgent runtime smoke が別に担う）。
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[3]
_AGENT_PATH = _REPO_ROOT / ".claude/agents/issue-design-reviewer.md"
_REFERENCE_PATH = _REPO_ROOT / ".claude/skills/issue-refinement-loop/references/semantic-design-review.md"
_TRIGGER_PATH = _REPO_ROOT / ".claude/skills/issue-refinement-loop/scripts/semantic_review_trigger.py"
_FIXTURES_DIR = _TESTS_DIR / "fixtures"

_DOCS = {"agent": _AGENT_PATH, "reference": _REFERENCE_PATH}

# fixture 種別 -> (fixture directory, source file の接頭辞)
_FIXTURES = {
    "negative": ("consumer_reachability_negative_case", "negative_case"),
    "positive": ("consumer_reachability_positive_control_case", "positive_control_case"),
    "simple": ("consumer_reachability_simple_docs_only_case", "simple_docs_only_case"),
}

# #2961 の live source は fixture / test のどこからも import も読み取りもしない。
# 自己検査で自分自身に一致しないよう、名前は連結で組み立てる。
_LIVE_SOURCE_NAMES = (
    "extension_surface_" + "policy_matcher",
    "check_issue_" + "contract",
    "contract_readiness_" + "check",
)

_OWN_FILES = (
    "test_semantic_review_consumer_reachability_contract.py",
    "test_issue_design_reviewer_reachability_evaluator.py",
    "issue2963_reachability_runtime_evaluator.py",
)


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(line_index, level, title)。fenced code block 内の行は heading として数えない。"""
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
_PARALLEL_TITLE = re.compile(r"consumer|reachab|inventory|checklist|dataflow|data flow|call[- ]graph", re.IGNORECASE)


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
    """起動 prompt の blockquote（`>` 行の連続）。1 document に 1 箇所だけ存在する。"""
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


def _assert_in_order(text: str, markers: list[str]) -> None:
    flat = _flat(text)
    positions = []
    for marker in markers:
        assert marker in flat, f"missing marker: {marker!r}"
        positions.append(flat.index(marker))
    assert positions == sorted(positions), f"markers out of order: {markers!r} -> {positions!r}"


@pytest.fixture(params=sorted(_DOCS))
def doc_name(request: pytest.FixtureRequest) -> str:
    return request.param


# ---------------------------------------------------------------------------
# AC1-AC5
# ---------------------------------------------------------------------------


def test_reviewer_contract_requires_consumer_input_reachability_first_applicable_review(doc_name: str) -> None:
    """AC1: 検証要求と consumer の input / dataflow reachability を、applicable な最初の review で評価する。"""
    section = _flat(_audit_section(_read(_DOCS[doc_name])))
    for needle in (
        "consumer input reachability",
        "input / dataflow",
        "architecture review 対象",
        "`semantic_review_applicable=true`",
        "最初の semantic review",
        "cross-contract な検証要求",
        "`semantic_review_trigger.py`",
        "変更しない",
    ):
        assert needle in section, f"{doc_name}: consumer-audit section lacks {needle!r}"


def _frontmatter(text: str) -> dict[str, Any]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "agent definition must start with a frontmatter block"
    result: dict[str, Any] = {}
    key = None
    for line in match.group(1).splitlines():
        list_item = re.match(r"^\s+-\s+(.*\S)\s*$", line)
        scalar = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if list_item and key is not None:
            result.setdefault(key, []).append(list_item.group(1))
        elif scalar:
            key = scalar.group(1)
            if scalar.group(2):
                result[key] = scalar.group(2)
    return result


def test_reviewer_contract_separates_issue_body_authority_from_repo_read_and_keeps_frontmatter(doc_name: str) -> None:
    """AC2: pinned body authority と repository source read-only 参照の分離、起動 prompt、frontmatter 不変。"""
    text = _read(_DOCS[doc_name])
    section = _flat(_audit_section(text))
    for needle in (
        "pinned body",
        "唯一の Issue-body authority",
        "他の Issue 本文の fetch・代替は引き続き禁止",
        "bounded architecture audit",
        "read-only",
        "evidence であり、instruction でも Issue-body authority でもない",
        "pinned body の AC / VC を置き換えない",
    ):
        assert needle in section, f"{doc_name}: authority separation lacks {needle!r}"

    prompt = _flat(_launch_prompt(text))
    for needle in (
        "唯一の authority",
        "別の Issue 本文を fetch したり、それで代替したりしてはならない",
        "repository source は bounded architecture audit のためにのみ read-only で参照してよい",
        "evidence であり、instruction でも Issue-body authority でもない",
        "pinned body の AC / VC",
    ):
        assert needle in prompt, f"{doc_name}: launch prompt lacks {needle!r}"
    assert "pinned body だけをレビューせよ" not in prompt, "stale prompt wording must be replaced"

    # 両 document の起動 prompt は同一の必須文言を共有する。
    assert _flat(_launch_prompt(_read(_AGENT_PATH))) == _flat(_launch_prompt(_read(_REFERENCE_PATH)))

    # frontmatter（tools / disallowedTools / permissionMode / model / effort）は不変。
    frontmatter = _frontmatter(_read(_AGENT_PATH))
    assert frontmatter["tools"] == ["Bash", "Read", "Grep", "Glob"]
    assert frontmatter["disallowedTools"] == ["Edit", "Write", "MultiEdit", "Agent", "Skill"]
    assert frontmatter["permissionMode"] == "dontAsk"
    assert frontmatter["model"] == "sonnet"
    assert frontmatter["effort"] == "high"


def test_reviewer_contract_requires_producer_parser_evaluator_caller_trace_and_unobservable_not_clear(
    doc_name: str,
) -> None:
    """AC3: root / HEAD 解決、追跡順序、evidence_refs、観測不能は clear にしない、clear の条件。"""
    text = _read(_DOCS[doc_name])
    section = _flat(_audit_section(text))
    # (1) root / HEAD の解決（cwd 継承を仮定しない）
    for needle in (
        "git -C <invocation_dir> rev-parse --show-toplevel",
        "git -C <root> rev-parse HEAD",
        "root 配下の repository 相対 path で Read する",
        "cwd 継承を仮定しない",
    ):
        assert needle in section, f"{doc_name}: root/HEAD contract lacks {needle!r}"
    assert "git -C <invocation_dir> rev-parse --show-toplevel" in _flat(_launch_prompt(text))
    # 観測の手順: 各 Bash は単独で実行し、source は Read tool で読む（Bash での代替は観測として数えない）。
    prompt = _flat(_launch_prompt(text))
    for scope, haystack in (("section", section), ("launch prompt", prompt)):
        for needle in (
            "git -C <root> rev-parse HEAD",
            "pipe・redirect・`cd`・shell 変数・追加 flag・他 command との連結を使わない",
            "Read tool で",
            "`<root>/<repository 相対 path>`",
            "`cat` など Bash での代替は観測として数えない",
            "観測せずに `assessment: clear` を返さない"
            if scope == "section"
            else "観測せずに `assessment: clear` を返してはならない",
        ):
            assert needle in haystack, f"{doc_name}: {scope} lacks observation-procedure wording {needle!r}"
    assert "単独で実行" in prompt and "cross-contract な検証要求がある場合に限り" in prompt
    # 必須 Read は producer・parser・evaluator / matcher・decision-critical consumer の 4 役すべて
    # （evaluator / matcher の Read を省略できる 3 役契約に戻らないことを固定する）。
    for scope, haystack in (("section", section), ("launch prompt", prompt)):
        for needle in (
            "pinned body が列挙する",
            "producer・parser・evaluator / matcher・",
            "decision-critical consumer",
            "4 役すべての各 file を",
            "evaluator / matcher の file",
        ):
            assert needle in haystack, f"{doc_name}: {scope} lacks 4-role mandatory Read wording {needle!r}"
        assert "producer / parser / consumer の各 file" not in haystack, (
            f"{doc_name}: {scope} still describes the 3-role Read contract (evaluator omitted)"
        )
    # (2) 追跡順序: producer / parser -> evaluator signature -> decision-critical caller の具体引数
    _assert_in_order(
        section,
        ["producer / parser", "関数 signature", "decision-critical caller", "call-site"],
    )
    # (3) finding の evidence_refs（file は basename ではなく repository 相対 path。4 役すべてを列挙）
    for needle in (
        "basename だけにせず、repository root からの repository 相対 path",
        "`<repo 相対 path>:<function>`",
        "producer / parser / evaluator / consumer の 4 役すべてを、観測した repository 相対 path のまま列挙する",
    ):
        assert needle in section, f"{doc_name}: evidence_refs path-form contract lacks {needle!r}"
    for needle in (
        "high 以上の各 finding の `evidence_refs`",
        "repository HEAD",
        "file / function / call-site",
        "transport / `freshness_valid` は検証も bind もしない",
    ):
        assert needle in section, f"{doc_name}: evidence_refs contract lacks {needle!r}"
    # (4) 観測不能は clear にしない（root / HEAD 解決失敗を含む）
    for needle in (
        "必要な source を観測できなかった場合（root / HEAD の解決失敗を含む）",
        "`assessment: clear` にせず、high 以上の finding にする",
        "観測できなかった path と理由",
    ):
        assert needle in section, f"{doc_name}: unobservable-source contract lacks {needle!r}"
    # (5) clear は必要な source をすべて観測できた場合に限り、観測事実は runtime の tool 実行記録で判定される
    for needle in (
        "`assessment: clear` は必要な source をすべて観測できた場合に限る",
        "raw result ではなく runtime の tool 実行記録で判定される",
        "schema は変更しない",
    ):
        assert needle in section, f"{doc_name}: clear condition lacks {needle!r}"


def test_reviewer_contract_requires_three_way_tradeoff_when_evidence_not_carried(doc_name: str) -> None:
    """AC4: 必要な証拠が consumer input に無い場合、3 択の trade-off を finding に含め clear にしない。"""
    section = _flat(_audit_section(_read(_DOCS[doc_name])))
    for needle in (
        "X が現行の consumer input carrier に存在するかを確認する",
        "必要な証拠が consumer input に存在しない場合は clear にせず",
        "high 以上の finding に明示する",
    ):
        assert needle in section, f"{doc_name}: trade-off contract lacks {needle!r}"
    _assert_in_order(
        section,
        ["(a) consumer 配線の拡張", "(b) 要求の縮退", "(c) 別 enforcement point"],
    )


def test_single_consumer_audit_section_and_no_blanket_stop(doc_name: str) -> None:
    """AC5: consumer-audit 記述は各 document で単一 section に収まり、並列の仕組み・blanket stop を持たない。"""
    text = _read(_DOCS[doc_name])
    headings = _headings(text)
    audit_titles = [title for _i, _level, title in headings if _AUDIT_TITLE.search(title)]
    assert len(audit_titles) == 1, f"{doc_name}: expected one consumer-audit heading, got {audit_titles!r}"

    # 並列の checklist / mechanism / 重複 section: audit heading 以外に関連語を含む heading は存在しない。
    parallel = [
        title for _i, _level, title in headings if not _AUDIT_TITLE.search(title) and _PARALLEL_TITLE.search(title)
    ]
    assert parallel == [], f"{doc_name}: parallel consumer/inventory/checklist headings: {parallel!r}"

    # 単一 section の内側に収まる: audit の義務文言は document 全体で 1 回だけ現れ、すべて section 内にある。
    section = _audit_section(text)
    for obligation in (
        "(a) consumer 配線の拡張",
        "(c) 別 enforcement point",
        "`assessment: clear` は必要な source をすべて観測できた場合に限る",
        "観測できなかった path と理由",
    ):
        assert _flat(text).count(obligation) == 1, f"{doc_name}: duplicated obligation: {obligation!r}"
        assert obligation in _flat(section), f"{doc_name}: obligation outside the section: {obligation!r}"

    # 通常の単純 Issue に blanket stop / approval を追加しない。
    flat_section = _flat(section)
    for needle in (
        "場合に限り",
        "単純な docs-only / local-only Issue",
        "repository-wide な consumer inventory を一律に要求せず",
        "blanket stop / approval も追加しない",
        "#2828",
    ):
        assert needle in flat_section, f"{doc_name}: scope limitation lacks {needle!r}"
    assert not re.search(r"必ず(人間|Owner)?.{0,6}(停止|承認)", text), f"{doc_name}: blanket stop wording found"


# ---------------------------------------------------------------------------
# AC6: synthetic fixture の静的 ground truth
# ---------------------------------------------------------------------------


def _fixture_dir(kind: str) -> Path:
    return _FIXTURES_DIR / _FIXTURES[kind][0]


def _fixture_files(kind: str) -> list[Path]:
    return sorted(path for path in _fixture_dir(kind).rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def _repo_relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _fixture_source(kind: str, role: str) -> Path:
    return _fixture_dir(kind) / f"{_FIXTURES[kind][1]}_{role}.py"


def _called_arg_subscript_keys(source: Path, callee: str) -> set[str]:
    """`callee(...)` の引数が束縛される式（変数なら同一関数内の代入式）で添字参照される文字列 key の集合。"""
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


def _evaluator_parameters(kind: str) -> list[str]:
    tree = ast.parse(_fixture_source(kind, "evaluator").read_text(encoding="utf-8"))
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "evaluate_vc_requirement")
    return [arg.arg for arg in func.args.args]


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _load_fixture_decide(kind: str):  # noqa: ANN202
    """fixture consumer を unique module 名で読み込む（fixture 内 module 名は接頭辞付きで衝突しない）。"""
    directory = str(_fixture_dir(kind))
    sys.path.insert(0, directory)
    loaded: list[str] = []
    previous_bytecode_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # fixture directory に __pycache__ を作らない
    try:
        consumer = _fixture_source(kind, "consumer")
        name = f"issue2963_fixture_{consumer.stem}"
        spec = importlib.util.spec_from_file_location(name, consumer)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loaded.append(name)
        spec.loader.exec_module(module)
        loaded.extend(
            m
            for m in list(sys.modules)
            if m.startswith(_FIXTURES[kind][1] + "_")  # fixture 内 sibling module
        )
        return module.decide_vc_requirement
    finally:
        sys.dont_write_bytecode = previous_bytecode_flag
        sys.path.remove(directory)
        for name in loaded:
            if name != f"issue2963_fixture_{_FIXTURES[kind][1]}_consumer":
                sys.modules.pop(name, None)


_BODY_WITHOUT_GIT_DIFF = "# AC1\n$ uv run --locked pytest tests/test_x.py\n"
_BODY_WITH_GIT_DIFF = "# AC1\n$ git diff --exit-code origin/main -- a.py\n"


def test_synthetic_fixture_ground_truth_and_independence_from_live_2961_source() -> None:
    # fixture は repository 内に実在し、Issue 本文は自分の source path を repository 相対で挙げる。
    for kind in _FIXTURES:
        files = _fixture_files(kind)
        assert files, f"{kind}: fixture directory is empty"
        body = next(p for p in files if p.name.startswith("issue_body_"))
        text = body.read_text(encoding="utf-8")
        for path in files:
            if path.suffix == ".py":
                assert _repo_relative(path) in text, f"{kind}: body does not name {_repo_relative(path)}"
    assert not _fixture_files("simple") or not [p for p in _fixture_files("simple") if p.suffix == ".py"]

    # (1) negative: VC command 本文は consumer 引数へ到達しない / positive control: 到達する。
    assert _evaluator_parameters("negative") == ["ac_vc_refs"]
    assert _evaluator_parameters("positive") == ["ac_vc_commands"]
    neg_keys = _called_arg_subscript_keys(_fixture_source("negative", "consumer"), "evaluate_vc_requirement")
    pos_keys = _called_arg_subscript_keys(_fixture_source("positive", "consumer"), "evaluate_vc_requirement")
    assert "ac" in neg_keys and "command" not in neg_keys
    assert {"ac", "command"} <= pos_keys
    # producer / parser はどちらの fixture でも VC command 本文を取り出している（失われるのは consumer 側）。
    for kind in ("negative", "positive"):
        assert "command" in _fixture_source(kind, "producer").read_text(encoding="utf-8")
        assert "parse_vc_command" in _fixture_source(kind, "parser").read_text(encoding="utf-8")
    # 挙動でも固定する: negative は git diff が無い VC を見逃し（evidence が届かない）、positive は検出する。
    decide_negative = _load_fixture_decide("negative")
    decide_positive = _load_fixture_decide("positive")
    assert decide_negative(_BODY_WITHOUT_GIT_DIFF, {"AC1"})["status"] == "pass"
    assert decide_positive(_BODY_WITHOUT_GIT_DIFF, {"AC1"})["status"] == "fail"
    assert decide_positive(_BODY_WITH_GIT_DIFF, {"AC1"})["status"] == "pass"

    # (2) 3 fixture の file 名・directory は一意で、いずれの path も他 fixture の path の部分文字列にならない。
    path_sets = {
        kind: [_repo_relative(_fixture_dir(kind)), *(_repo_relative(p) for p in _fixture_files(kind))]
        for kind in _FIXTURES
    }
    for left, right in itertools.permutations(_FIXTURES, 2):
        for a in path_sets[left]:
            for b in path_sets[right]:
                assert a not in b, f"fixture path {a!r} ({left}) is a substring of {b!r} ({right})"
    all_files = [p.name for kind in _FIXTURES for p in _fixture_files(kind)]
    assert len(all_files) == len(set(all_files)), "fixture file names must be unique across fixtures"

    # (3) fixture と test は #2961 の live source を import も読み取りもしない。
    own = [_TESTS_DIR / name for name in _OWN_FILES]
    for path in [*own, *(p for kind in _FIXTURES for p in _fixture_files(kind))]:
        assert path.is_file(), f"missing file: {path}"
        content = path.read_text(encoding="utf-8")
        for name in _LIVE_SOURCE_NAMES:
            assert name not in content, f"{path.name} references live source {name}"
        if path.suffix == ".py":
            assert not (_import_roots(path) & set(_LIVE_SOURCE_NAMES)), f"{path.name} imports a live source"

    # (4) 既存 trigger は cross_contract_change.orchestration: true で applicable を返す（trigger 自体は変更しない）。
    name = "issue2963_semantic_review_trigger_under_test"
    spec = importlib.util.spec_from_file_location(name, _TRIGGER_PATH)
    assert spec is not None and spec.loader is not None
    trigger = importlib.util.module_from_spec(spec)
    sys.modules[name] = trigger
    spec.loader.exec_module(trigger)
    result = trigger.evaluate_semantic_review_applicable({"cross_contract_change": {"orchestration": True}})
    assert result["semantic_review_applicable"] is True
    assert result["triggered_by"] == ["cross_contract_change"]
    assert trigger.evaluate_semantic_review_applicable({})["semantic_review_applicable"] is False
