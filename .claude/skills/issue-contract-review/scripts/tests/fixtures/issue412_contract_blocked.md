## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "999"
goal_ref: "synthetic fixture for contract readiness test — blocked case"
change_kind: workflow
```

## Parent Issue

#999

## Outcome

A script exists that can verify the contract.

## In Scope

- Add script
- Add tests

## Out of Scope

- Deployment

## Acceptance Criteria

- [ ] AC1: Script exists <!-- runtime-verification: true -->
- [ ] AC2: Script returns valid JSON <!-- runtime-verification: true -->
- [ ] AC3: Compound command is detected <!-- runtime-verification: true -->
- [ ] AC4: RVA immediate field missing is detected <!-- runtime-verification: true -->

## Verification Commands

```bash
# AC1
$ test -f some_script.py

# AC2
# compound command — violates VC_SINGLE_COMMAND_GUARDRAIL
$ uv run python3 some_script.py && echo PASS

# AC3
$ test -f .claude/skills/issue-contract-review/scripts/contract_readiness_check.py

# AC4
$ uv run pytest .claude/skills/issue-contract-review/scripts/tests/ -q -k "contract_readiness"
```

## Allowed Paths

```
.claude/skills/issue-contract-review/scripts/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
  - AC2
execution_environment:
  cli_tools:
    - python3
    - uv
```

