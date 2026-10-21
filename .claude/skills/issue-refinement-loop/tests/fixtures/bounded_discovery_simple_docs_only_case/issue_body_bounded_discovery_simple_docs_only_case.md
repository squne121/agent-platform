## 機械可読の契約（Machine-Readable Contract）

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: docs
```

## Outcome

合成 Issue（bounded discovery simple docs-only fixture）: 用語メモ `.claude/skills/issue-refinement-loop/tests/fixtures/bounded_discovery_simple_docs_only_case/qsimple_glossary.md`
に「pinned body」の 1 行説明を追加する。他の contract・validator・schema には関与しない。

## Acceptance Criteria（受け入れ条件）

- [ ] AC1: 用語メモに「pinned body」という語の説明が 1 行追加されている。

## Verification Commands（検証コマンド）

```bash
# AC1
$ rg -n "pinned body" .claude/skills/issue-refinement-loop/tests/fixtures/bounded_discovery_simple_docs_only_case/qsimple_glossary.md
```

## Allowed Paths（変更許可パス）

- `.claude/skills/issue-refinement-loop/tests/fixtures/bounded_discovery_simple_docs_only_case/qsimple_glossary.md`（変更を許可）
