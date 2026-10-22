# Product Spec Preflight

## 適用条件

以下のいずれかで実行:

- `docs/product/**` が Allowed Paths に含まれる
- `tasks.md` / `.specify/` / `generated_task_mentioned: true` / `source_task_id`
- `## Product Spec Context` が存在

## 判定コマンド

```bash
uv run python3 .claude/skills/issue-contract-review/scripts/check_product_spec_contract.py \
  --issue-number <番号> \
  --repo <owner>/<repo>
```

## Rule ID

- `PS001` docs/product update のリンク整合
- `PS002` `tasks.md` が staging artifact として参照されている
- `PS003` `.specify/` が workbench 文脈で使われている
- `PS004` spec 更新 evidence（rationale / changed_requirement_id / affected_sections）
- `PS005` generated task の `requirement_id` / `source_task_id`
- `PS006` generated task が materialize (`Depends on #N`) 済み

## 判定出力

- `applicability: applicable` かつ `decision: pass` → 次ステップへ
- `applicability: applicable` かつ `decision: human_judgment` / `fail` → BLOCKED（`issue-refinement-loop` 推奨）
- `applicability: not_applicable` かつ `pass` → 次ステップへ
