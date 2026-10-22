---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: plus bullet fixture (warning expected)
---
<!-- Fixture 3: plus bullet marker "+" (Blocker 2).
CVS has ".claude/skills/foo/SKILL.md", In Scope has "docs/foo.md" → different paths → warning fires. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "plus bullet warning test"
change_kind: code
```

## Outcome

スキルを docs 化する。

## Current Validated Scope

+ .claude/skills/foo/SKILL.md を更新する
+ .claude/skills/bar/SKILL.md を追加する

## In Scope

+ docs/foo.md を新規追加する
+ docs/bar.md を整備する

## Acceptance Criteria

- [ ] AC1: `docs/foo.md` が存在する

## Verification Commands

```bash
# AC1
$ test -f docs/foo.md
```

## Stop Conditions

- 1
- 2
- 3
- 4
- 5
- 6

## Runtime Verification Applicability

decision: not_applicable
reason: fixture only.

## Allowed Paths

- `docs/foo.md`
