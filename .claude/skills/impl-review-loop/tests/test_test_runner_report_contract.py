"""Issue #2892: test-runner の TEST_VERDICT_MACHINE/v2 報告契約を consumer 要件に整合させる契約テスト。

`.claude/agents/test-runner.md` の machine-valid example を coercion / 正規化なしで既存 consumer
（`adjudicate_vc_result.py` の `adapt_test_verdict_to_current_vc_result()` と canonical adjudicate 入口）へ
そのまま渡し、canonical PASS になること、欠落・不正が fail-closed のままであることを固定する。
consumer・Step 2 文書・runtime smoke runner は read-only の検証対象であり、本テストは変更しない。
"""

from __future__ import annotations

import copy
import datetime
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts" / "adjudicate_vc_result.py"
TEST_RUNNER_MD = ROOT / ".claude" / "agents" / "test-runner.md"
SMOKE_PROMPT = (
    ROOT / ".claude" / "skills" / "impl-review-loop" / "tests" / "fixtures" / "test_runner_report_smoke_prompt.md"
)

# 同名 module との sys.modules 衝突を避けるため、一意名で spec_from_file_location 経由の読み込みを行う。
_MODULE_NAME = "adjudicate_vc_result_test_runner_report_contract_2892"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]

RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_EXACT_FORM = "date -u +%Y-%m-%dT%H:%M:%SZ"
GITHUB_DERIVED_TOP_LEVEL_KEYS = (
    "producer_kind",
    "repository",
    "run_id",
    "run_url",
    "workflow_run_id",
    "workflow_run_attempt",
    "check_run_id",
)

# example の 3 case（通常 AC / literal AC_UNKNOWN / カンマ連結ラベル）に対応する live Issue 本文の VC 部。
EXAMPLE_VC_BODY = """\
## Verification Commands

```bash
# AC1
$ ls .claude/agents/test-runner.md
$ uv run --locked ruff check .
# AC1, AC2
$ uv run --locked ruff format --check .
```
"""
EXPECTED_AC_LABELS = ["AC1", "AC_UNKNOWN", "AC1,AC2"]


def _text() -> str:
    return TEST_RUNNER_MD.read_text(encoding="utf-8")


def _section(text: str, start_heading: str, end_heading: str | None) -> str:
    start = text.index(start_heading)
    end = text.index(end_heading, start + len(start_heading)) if end_heading else len(text)
    return text[start:end]


def _fenced_blocks(text: str, lang: str) -> list[str]:
    return re.findall(r"```" + re.escape(lang) + r"\n(.*?)```", text, flags=re.DOTALL)


def _example_source() -> str:
    section = _section(_text(), "### machine-valid example", "### `generated_at` の規約")
    blocks = _fenced_blocks(section, "yaml")
    assert len(blocks) == 1, "machine-valid example section must contain exactly one yaml block"
    return blocks[0]


def _load_example() -> dict[str, Any]:
    """example を coercion / 正規化なしで parse する（yaml.safe_load の戻り値をそのまま使う）。"""
    loaded = yaml.safe_load(_example_source())
    assert isinstance(loaded, dict) and isinstance(loaded.get("TEST_VERDICT"), dict)
    return loaded


def _baseline_snapshot(expected_rows: list[dict[str, Any]], body_sha256: str) -> dict[str, Any]:
    classifications = [
        {
            "ac": row["ac"],
            "command_hash": row["command_hash"],
            "raw_command": row["raw_command"],
            "classification": "expected_pass",
            "exit_code": 0,
            "failure_keys": [],
        }
        for row in expected_rows
    ]
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": classifications}},
    }


def _baseline_rows() -> list[dict[str, Any]]:
    rc, payload = mod.extract_vc_metadata(EXAMPLE_VC_BODY)
    assert rc == 0 and payload["status"] == "ok"
    return payload["commands"]


def _adjudicate(report: dict[str, Any]) -> dict[str, Any]:
    """report を adapt し（変換のみ・補完なし）、canonical adjudicate 入口へ渡す。"""
    verdict = report["TEST_VERDICT"]
    current, _errors = mod.adapt_test_verdict_to_current_vc_result(report)
    assert current is not None
    snapshot = _baseline_snapshot(_baseline_rows(), verdict["contract_body_sha256"])
    return mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary={"changed_paths": [".claude/agents/test-runner.md"], "head_sha": verdict["head_sha"]},
        allowed_paths=[".claude/agents/test-runner.md"],
    )


def _is_canonical_pass(result: dict[str, Any]) -> bool:
    return (
        result["overall_status"] == "pass"
        and result["blocking"] is False
        and result["rerun_required"] is False
        and result["errors"] == []
        and all(entry["status"] == "pass" and entry["blocking"] is False for entry in result["per_ac"])
    )


# ---------------------------------------------------------------------------
# AC1: grammar と machine-valid example の分離、generated_at の規約
# ---------------------------------------------------------------------------


def test_machine_valid_example_generated_at_is_quoted_rfc3339_string_and_separate_from_grammar():
    text = _text()
    grammar = _section(text, "### field grammar", "### machine-valid example")
    example_src = _example_source()

    # grammar は説明用 placeholder を持つ別ブロック。machine-valid example には placeholder が無い。
    assert "<PASS | PARTIAL | FAIL" in grammar and "<bool>" in grammar
    for placeholder in ("PASS | PARTIAL | FAIL", "true | false", "<int>", "<owner/repo>"):
        assert placeholder not in example_src
    assert not re.search(r"<[^>\n]+>", example_src)
    assert example_src not in grammar

    report = _load_example()["TEST_VERDICT"]
    generated_at = report["generated_at"]
    assert type(generated_at) is str and RFC3339_UTC_RE.match(generated_at)
    assert not isinstance(generated_at, datetime.datetime)
    assert re.search(r'^\s+generated_at: "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"$', example_src, flags=re.MULTILINE)
    # coercion なしで JSON 化できる（default= を使わない）。
    json.dumps(_load_example())

    # 型が確定している。
    assert type(report["issue_number"]) is int and type(report["pr_number"]) is int
    for row in report["runtime_ac_results"]:
        assert type(row["exit_code"]) is int
        for flag in ("fallback_detected", "human_review_required", "stop_condition_triggered"):
            assert type(row[flag]) is bool
        for key in ("ac", "command", "command_hash", "status", "notes"):
            assert type(row[key]) is str

    # 規約節: 生成時刻・exact date 形・代用禁止。
    convention = _section(text, "### `generated_at` の規約", "### Step 2 委譲契約")
    assert "test-runner がこの report を生成した UTC 時刻" in convention
    assert DATE_EXACT_FORM in convention
    assert "推測値" in convention and "固定値" in convention and "root の受領時刻" in convention
    assert "禁止" in convention


def test_machine_valid_example_generated_at_unquoted_timestamp_would_break_json():
    """引用が必須である理由: 無引用の RFC 3339 時刻は PyYAML が datetime に解決し、JSON 化できない。"""
    unquoted = yaml.safe_load("generated_at: 2026-10-04T09:30:00Z")
    assert isinstance(unquoted["generated_at"], datetime.datetime)
    try:
        json.dumps(unquoted)
    except TypeError:
        pass
    else:  # pragma: no cover - 失敗時のみ到達
        raise AssertionError("an unquoted timestamp must not be JSON serializable without default=")


# ---------------------------------------------------------------------------
# AC2: command policy の narrow allowance
# ---------------------------------------------------------------------------


def test_command_policy_narrow_allowances():
    text = _text()
    policy = _section(text, "## 許可するコマンド", "### Issue #2656 限定の狭域例外（正規委譲経路のランタイム検証）")
    allowed_block = _fenced_blocks(policy, "")[0]
    allowed_lines = [line.split("#", 1)[0].strip() for line in allowed_block.splitlines()]
    allowed_lines = [line for line in allowed_lines if line]

    # date は exact 形のみ。
    date_lines = [line for line in allowed_lines if line.startswith("date")]
    assert date_lines == [DATE_EXACT_FORM]
    # pytest は repo-relative target 付きの 2 行のみ。target 省略形・任意 uv run・git・書込みは許可行に無い。
    pytest_lines = [line for line in allowed_lines if "pytest" in line]
    assert pytest_lines == [
        "uv run --locked pytest <repo-relative target> [pytest args]",
        "uv run pytest <repo-relative target> [pytest args]",
    ]
    for line in allowed_lines:
        assert not line.startswith("git")
        assert not line.startswith("uv run python")
        assert not re.search(r"(^|\s)(tee|sed -i|rm|mv|cp)(\s|$)", line)
        assert not re.search(r"(^|\s)>>?(\s|$)", line)  # shell redirect

    # Issue #2467 AC8 の旧規則を VC 逐語 target へ置換し、supersede と OWNER 指示 URL を明記する。
    assert "Verification Commands` に **逐語で記載された repo-relative の pytest target**" in policy
    assert "Issue #2467 AC8" in policy and "supersede" in policy
    assert "issues/2892#issuecomment-5978351466" in policy
    assert "上記 2 行は、Issue の Allowed Paths 内の repo-relative なテスト対象" not in policy

    # 引き続き許可しないもの（pytest 規則節と date 規則節の双方で明記）。
    for forbidden in (
        "target なしの `pytest` / `uv run pytest`",
        "`uv run python3 -c",
        "ファイル書込み、git 操作",
        "リポジトリ外のパス",
    ):
        assert forbidden in policy, forbidden
    date_rule = _section(policy, "#### `date` 実行規則", None)
    assert DATE_EXACT_FORM in date_rule
    assert "他の `date` 形式" in date_rule and "許可しない" in date_rule

    # 「実行してはいけないコマンド」節にも一般化しない旨がある。
    forbidden_section = _section(text, "## 実行してはいけないコマンド", "## Mergeable 状態の検知")
    assert "`uv run python3 ...`" in forbidden_section
    assert "`date -u +%Y-%m-%dT%H:%M:%SZ` 以外の `date`" in forbidden_section
    assert "git add" in forbidden_section


# ---------------------------------------------------------------------------
# AC3: GitHub 由来 field の適用条件分離
# ---------------------------------------------------------------------------


def test_conditional_github_fields_separation_and_no_unconditional_statement():
    text = _text()
    grammar = _section(text, "### field grammar", "### machine-valid example")
    independent, github = grammar.split("**(2) GitHub 由来 field 群**", 1)
    assert "独立実行 field 群" in independent
    for field in ("schema", "issue_number", "pr_number", "head_sha", "reviewed_head_sha", "diff_head_sha",
                  "contract_body_sha256", "generated_at", "result", "runtime_ac_results"):
        assert f"{field}:" in independent
    for field in GITHUB_DERIVED_TOP_LEVEL_KEYS + ("artifact:", "artifact_payload:", "artifact_payload_sha256:"):
        assert field in github
        assert field not in independent
    first_line = github.splitlines()[0]
    assert "`pr_review_only`" in first_line and "legacy publish" in first_line and "でのみ必須" in first_line
    assert "捏造しない" in github

    # machine-valid example は GitHub 由来 field を含まない。
    report = _load_example()["TEST_VERDICT"]
    for key in report:
        assert key not in GITHUB_DERIVED_TOP_LEVEL_KEYS
        assert not key.startswith("artifact")
    for row in report["runtime_ac_results"]:
        assert not any(key.startswith("artifact") and key != "artifact_present" for key in row)

    # 無条件の「全フィールド必須」記述は残らない。出力制約節は適用条件付きの記述になる。
    assert "全フィールドは必ず含める" not in text
    assert not re.search(r"全フィールド[^\n]*routing 必須フィールド", text)
    budget = _section(text, "## 出力制約 (OUTPUT_BUDGET_V1)", "## VC 逐語実行規則")
    assert "独立実行 field 群" in budget and "GitHub 由来 field 群" in budget
    assert "`pr_review_only`" in budget and "legacy publish" in budget
    assert "でのみ必須" in budget and "捏造せず" in budget


# ---------------------------------------------------------------------------
# AC4: example が coercion なしで consumer の canonical PASS になる
# ---------------------------------------------------------------------------


def test_independent_report_canonical_pass_verbatim():
    report = _load_example()
    verdict = report["TEST_VERDICT"]
    assert type(verdict["generated_at"]) is str
    json.dumps(report)  # default= なし

    converted, errors = mod.adapt_test_verdict_to_current_vc_result(report)
    assert errors == [] and converted is not None
    assert converted["generated_at"] == verdict["generated_at"] and type(converted["generated_at"]) is str
    assert converted["status"] == "pass"
    json.dumps(converted)  # adapt 後も default= なしで JSON 化できる

    result = _adjudicate(report)
    assert _is_canonical_pass(result), result
    assert [entry["ac"] for entry in result["per_ac"]] == EXPECTED_AC_LABELS

    # step4 gate（pr-reviewer 起動可否）も同じ binding で開く。
    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=verdict["head_sha"],
        expected_contract_body_sha256=verdict["contract_body_sha256"],
        expected_command_hashes=[row["command_hash"] for row in _baseline_rows()],
    )
    assert gate == {"invoke_pr_reviewer": True, "reason_code": None}

    # JSON 経由の CLI 形（adapt サブコマンドが読む形）でも同一の変換結果になる。
    assert mod.adapt_test_verdict_to_current_vc_result(json.loads(json.dumps(report)))[0] == converted


def _mutated(mutator) -> dict[str, Any]:
    report = copy.deepcopy(_load_example())
    mutator(report["TEST_VERDICT"])
    return report


def _assert_not_pass(result: dict[str, Any]) -> None:
    assert not _is_canonical_pass(result), result
    assert result["overall_status"] != "pass"
    assert result["per_ac"] == [] or any(entry["status"] != "pass" for entry in result["per_ac"]) or result["errors"]


def test_independent_report_canonical_pass_verbatim_fail_closed_negative_cases():
    # generated_at 欠落 -> uncertified_current_pass
    missing = _adjudicate(_mutated(lambda v: v.pop("generated_at")))
    _assert_not_pass(missing)
    assert missing["blocking"] is True
    assert {entry["reason_code"] for entry in missing["per_ac"]} == {"uncertified_current_pass"}

    # generated_at 空文字 -> uncertified_current_pass
    empty = _adjudicate(_mutated(lambda v: v.__setitem__("generated_at", "")))
    _assert_not_pass(empty)
    assert empty["blocking"] is True
    assert {entry["reason_code"] for entry in empty["per_ac"]} == {"uncertified_current_pass"}

    # result: FAIL は PASS にならない。
    failed = _adjudicate(_mutated(lambda v: v.__setitem__("result", "FAIL")))
    _assert_not_pass(failed)
    assert failed["blocking"] is True

    # command_hash 不一致 -> baseline との対応が成立せず fail-closed。
    def _bad_hash(v: dict[str, Any]) -> None:
        v["runtime_ac_results"][0]["command_hash"] = "sha256:" + "f" * 64

    bad_hash = _adjudicate(_mutated(_bad_hash))
    _assert_not_pass(bad_hash)
    assert bad_hash["errors"] == ["baseline_current_mapping_mismatch"]

    # fallback_detected: true の行は PASS にならない（consumer の safety check は緩和されていない）。
    def _fallback(v: dict[str, Any]) -> None:
        v["runtime_ac_results"][1]["fallback_detected"] = True

    _assert_not_pass(_adjudicate(_mutated(_fallback)))


# ---------------------------------------------------------------------------
# AC5: (ac, command, command_hash) の 1 command = 1 行 逐語 echo
# ---------------------------------------------------------------------------


def test_ac_command_hash_mapping_cases():
    report = _load_example()["TEST_VERDICT"]
    rows = report["runtime_ac_results"]
    baseline_rows = _baseline_rows()

    # 3 case（通常 AC / literal AC_UNKNOWN / カンマ連結ラベル）を 1 command = 1 行で持つ。
    assert [row["ac"] for row in rows] == EXPECTED_AC_LABELS
    assert len(rows) == len(baseline_rows) == 3
    # (ac, command, command_hash) が、共有 helper で独立導出した baseline と 1 対 1 で逐語一致する。
    assert [(r["ac"], r["command"], r["command_hash"]) for r in rows] == [
        (b["ac"], b["raw_command"], b["command_hash"]) for b in baseline_rows
    ]

    # 圧縮ラベル AC1-AC2 は baseline_current_mapping_mismatch で fail-closed。
    def _compress(v: dict[str, Any]) -> None:
        assert v["runtime_ac_results"][2]["ac"] == "AC1,AC2"
        v["runtime_ac_results"][2]["ac"] = "AC1-AC2"

    compressed = _adjudicate(_mutated(_compress))
    _assert_not_pass(compressed)
    assert compressed["errors"] == ["baseline_current_mapping_mismatch"]
    assert compressed["overall_status"] == "indeterminate"

    # literal AC_UNKNOWN を別 label へ再帰属しても fail-closed。
    def _reattribute(v: dict[str, Any]) -> None:
        v["runtime_ac_results"][1]["ac"] = "AC3"

    assert _adjudicate(_mutated(_reattribute))["errors"] == ["baseline_current_mapping_mismatch"]

    # 行の欠落（1 対 1 対応の崩れ）も fail-closed。
    def _drop_row(v: dict[str, Any]) -> None:
        v["runtime_ac_results"].pop()

    assert _adjudicate(_mutated(_drop_row))["errors"] == ["baseline_current_mapping_mismatch"]

    # test-runner.md に同規約が明記されている。
    contract = _section(_text(), "### Step 2 委譲契約", "### report 各項目の補足")
    for token in (
        "逐語",
        "`AC_UNKNOWN`",
        "カンマ連結ラベル",
        "`AC1-AC8`",
        "`AC1-AC2`",
        "1 command = 1 行",
        "1 対 1",
        "`command_hash`",
        "`sha256:<hex>`",
        "`baseline_current_mapping_mismatch`",
    ):
        assert token in contract, token


# ---------------------------------------------------------------------------
# runtime smoke fixture prompt: 定数が共有 helper の導出値と一致し、marker 一覧を保持する
# ---------------------------------------------------------------------------

SMOKE_VC_BODY = """\
## Verification Commands

```bash
# AC1
$ echo test-runner-report-smoke-ac1
$ echo test-runner-report-smoke-unlabeled
# AC1, AC2
$ test -f .claude/agents/test-runner.md
```
"""


def test_smoke_fixture_prompt_constants_match_shared_helper_and_markers():
    prompt = SMOKE_PROMPT.read_text(encoding="utf-8")
    rc, payload = mod.extract_vc_metadata(SMOKE_VC_BODY)
    assert rc == 0 and payload["status"] == "ok"
    derived = [(c["ac"], c["raw_command"], c["command_hash"]) for c in payload["commands"]]
    assert [ac for ac, _, _ in derived] == EXPECTED_AC_LABELS

    tuple_lines = re.findall(
        r"^\d+\. ac: (\S+) \| command: (.+?) \| command_hash: (sha256:[0-9a-f]{64})$", prompt, flags=re.MULTILINE
    )
    assert tuple_lines == derived

    # fixture の command は test-runner の許可コマンドだけ（uv run python3 / git / 書込みを必要としない）。
    for _ac, command, _hash in derived:
        assert command.split()[0] in {"echo", "test"}
        assert not re.search(r"uv run|git |>|tee", command)
    assert DATE_EXACT_FORM in prompt

    marker_section = prompt.split("## Operator 用の期待 marker 一覧", 1)[1]
    markers = _fenced_blocks(marker_section, "text")[0].splitlines()
    expected = [
        "schema: TEST_VERDICT_MACHINE/v2",
        "result: PASS",
        'generated_at: "20',
        "fallback_detected: false",
        'status: "pass"',
        'ac: "AC1"',
        'ac: "AC_UNKNOWN"',
        'ac: "AC1,AC2"',
    ] + [f'command_hash: "{command_hash}"' for _ac, _cmd, command_hash in derived]
    assert markers == expected

    # marker は example の値水準 literal と整合する（example に同じ書式の行が存在する）。
    example_src = _example_source()
    for literal in expected[:8]:
        assert literal in example_src, literal
