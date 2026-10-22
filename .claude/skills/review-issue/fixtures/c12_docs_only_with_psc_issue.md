---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: M4 マイルストーン SSOT を docs 正本へ同期する
---

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "M4 マイルストーン記述を docs SSOT に同期する"
change_kind: docs
```

## Parent Issue

なし（docs SSOT 同期のための単独 Issue）

## Parent Goal Ref

- Goal: `docs/product/playable-roadmap.md` と `docs/dev/current-focus.md` の M4 記述差分を解消し、SSOT を一本化する
- Desired Destination: docs 間で M4 のマイルストーン定義が矛盾しない状態

## Outcome

M4 マイルストーンの記述が docs SSOT に同期され、`docs/product/playable-roadmap.md` を正本として `docs/dev/current-focus.md` の記述が一致する状態。

## In Scope

- `docs/product/playable-roadmap.md` の M4 セクションを正本として確定する
- `docs/dev/current-focus.md` の M4 記述を正本に合わせて同期する

## Out of Scope

- コードや systems 層の変更
- 新規マイルストーンの追加

## Product Spec Context
本セクションは docs SSOT 同期の参照元と差分理由を示す（docs-only であり、明示的な trace field は宣言しない）。

- source_of_truth: `docs/product/playable-roadmap.md`
- diff_rationale: `docs/dev/current-focus.md` の M4 記述が roadmap 正本と乖離しているため、正本へ同期する
- changed_requirement_id: `DOC-M4-001`
- affected_sections: `M4: Upgrade Loop` セクションのスコープ記述
- product_spec_change_mode: docs_ssot_sync_only

## Acceptance Criteria

- [ ] AC1: `docs/dev/current-focus.md` の M4 記述が `docs/product/playable-roadmap.md` の正本と一致する

## Verification Commands

```bash
# AC1
# baseline-expect: fail
$ rg -n "M4: Upgrade Loop" docs/dev/current-focus.md
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- コード変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- スコープ外への波及が判明した場合
- 仕様の固定契約変更が必要になった場合
- 外部サービス利用が必要になった場合

## Runtime Verification Applicability

- decision: not_applicable
- reason: docs 同期のみで、プロセス起動や I/O を伴う動作検証は不要。

## Allowed Paths

- `docs/dev/current-focus.md`
