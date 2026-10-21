## 機械可読の契約（Machine-Readable Contract）

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: workflow
```

## Outcome

合成 Issue（bounded discovery hybrid fixture）: 参照された AC の Verification Command が `git diff` を含むことを、shared evaluator が
実際の判定経路で検証する状態にする。既存の consumer 配線は参照 AC の集合を evaluator へ渡す。

## 参照するソース構成

- producer（証拠の生成元）: `.claude/skills/issue-refinement-loop/tests/fixtures/bounded_discovery_hybrid_case/qhyb_producer.py`
- parser（VC 行の分解）: `.claude/skills/issue-refinement-loop/tests/fixtures/bounded_discovery_hybrid_case/qhyb_parser.py`
- shared evaluator（検証責務を持つ関数）: 関数 `judge_qhyb_vc_requirement`（定義 file の名前に `qhyb_evaluator` を含む。repository 相対 path は本文に記載しない）
- decision-critical consumer（evaluator の呼び出し元）: 関数 `route_qhyb_vc_decision`（定義 file の名前に `qhyb_consumer` を含む。repository 相対 path は本文に記載しない）

## Acceptance Criteria（受け入れ条件）

- [ ] AC1: 参照 AC の VC command 本文に `git diff` が含まれない場合、shared evaluator が `fail` を返す。
- [ ] AC2: 参照 AC の VC command 本文に `git diff` が含まれる場合、shared evaluator が `pass` を返す。

## Verification Commands（検証コマンド）

```bash
# AC1
$ uv run --locked pytest tests/test_vc_requirement.py::test_missing_git_diff_fails
# AC2
$ uv run --locked pytest tests/test_vc_requirement.py::test_git_diff_present_passes
```

## Allowed Paths（変更許可パス）

- 上記 shared evaluator と decision-critical consumer の定義 file（変更を許可）
