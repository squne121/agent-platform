# Issue Contract Review: Contract Compliance Rules

## 2. 開発フロー適合性チェック

| 確認項目 | 判定 |
|---|---|
| テンプレ準拠 | `.github/ISSUE_TEMPLATE/{種別}.yml` の必須セクションが存在 |
| Allowed Paths 明示 | `## Allowed Paths` が存在し空でない |
| VC 明示 | `## Verification Commands` が存在し、1 以上ある |
| Stop Conditions | implementation のみ 6 項目が埋まっている |

fail 1 つでも `BLOCKED`。

> 重要（#2084 OWNER directive）: GitHub Issue label（`state/needs-human` を含む全 label）は
> presentation-only の non-authoritative metadata であり、readiness（`CONTRACT_REVIEW_RESULT_V1.status`）
> の算出に一切使わない。label→readiness の transitive coupling を上流（本 skill）に持ち込まない。
> `state_label` を observational telemetry として記録する場合も `status`（go/blocked/human_judgment）
> の判定には寄与させない。停止が必要な場合は label ではなく OWNER の明示指示や
> `human_judgment_required`（scope-signal-guard 等の別 authority）を根拠にする。readiness authority
> は Issue native open/closed state・Machine-Readable Contract・GitHub native dependency close 状態・
> explicit operator/OWNER directive・review/test/CI 結果である（#2084 Outcome の Readiness Plane 定義）。

## 3. blocker / dependency 全 close 確認

```bash
bash .claude/skills/issue-contract-review/scripts/check_blockers.sh <issue_number> <owner>/<repo>
```

- `native` API が primary。取得不可時のみ `Depends on #N` を fallback。
- native と fallback が不一致なら `human_escalation`。
- Blocker 開示:
  - blocker 番号の列挙
  - 不一致時の差分根拠
  - 次アクション（issue-refinement-loop 提案）

```text
Fallback parsing は `## Depends On` / `Depends on #<N>` の line-anchored 形式のみ。Delivery note や条件文は除外。
```

## 4. Worktree / Branch 事前チェック

- `.claude/worktrees/<slug>` の予約
- 既存 worktree / branch 衝突を確認
- 外部配置は NG

