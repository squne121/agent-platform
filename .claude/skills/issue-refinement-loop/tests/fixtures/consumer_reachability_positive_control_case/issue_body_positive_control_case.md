## 機械可読の契約（Machine-Readable Contract）

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: workflow
```

## Outcome

合成 Issue（positive control fixture）: 参照された AC の Verification Command が `git diff` を含むことを、shared evaluator が
実際の判定経路で検証する状態にする。consumer は参照 AC の VC command 本文（AC 番号 -> command）を evaluator へ渡す既存配線を持つ。

## 参照するソース構成（repository 相対 path）

- producer（証拠の生成元）: `.claude/skills/issue-refinement-loop/tests/fixtures/consumer_reachability_positive_control_case/positive_control_case_producer.py`
- parser（VC 行の分解）: `.claude/skills/issue-refinement-loop/tests/fixtures/consumer_reachability_positive_control_case/positive_control_case_parser.py`
- shared evaluator（検証責務を持つ関数）: `.claude/skills/issue-refinement-loop/tests/fixtures/consumer_reachability_positive_control_case/positive_control_case_evaluator.py`
- decision-critical consumer（evaluator の呼び出し元）: `.claude/skills/issue-refinement-loop/tests/fixtures/consumer_reachability_positive_control_case/positive_control_case_consumer.py`

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

- `.claude/skills/issue-refinement-loop/tests/fixtures/consumer_reachability_positive_control_case/positive_control_case_evaluator.py`（変更を許可）
- `.claude/skills/issue-refinement-loop/tests/fixtures/consumer_reachability_positive_control_case/positive_control_case_consumer.py`（変更を許可）
