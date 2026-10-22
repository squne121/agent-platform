## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "999"
goal_ref: "synthetic fixture for contract readiness test — go case"
change_kind: workflow
```

## Parent Issue

#999

## Outcome

A script exists and all fixture tests pass.

## In Scope

- Add contract_readiness_check.py
- Add pytest tests

## Out of Scope

- Deployment
- GitHub integration

## Acceptance Criteria

- [ ] AC1: `contract_readiness_check.py` exists <!-- runtime-verification: true -->
- [ ] AC2: script returns ISSUE_CONTRACT_READINESS_RESULT_V1 JSON <!-- runtime-verification: true -->
- [ ] AC3: static mode requires no network or auth <!-- runtime-verification: true -->

## Verification Commands

```bash
# AC1
# preflight-scope: runtime_only
$ test -f .claude/skills/issue-contract-review/scripts/contract_readiness_check.py

# AC2
# preflight-scope: runtime_only
$ uv run pytest .claude/skills/issue-contract-review/scripts/tests/ -q -k "contract_readiness_result_v1_schema"

# AC3
# preflight-scope: runtime_only
$ uv run pytest .claude/skills/issue-contract-review/scripts/tests/ -q -k "no_network_required"
```

## Allowed Paths

```
.claude/skills/issue-contract-review/scripts/
.claude/skills/issue-contract-review/scripts/tests/
.claude/skills/issue-contract-review/scripts/tests/fixtures/
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
  - AC3
execution_environment:
  cli_tools:
    - python3
    - uv
  auth_required: false
  network_required: false
skip_conditions:
  - "local repository checkout が存在しない場合は SKIP ではなく environment blocked として扱う"
fallback_policy:
  fallback_success_is_pass: false
  notes: "fallback 経由の成功を PASS に変換しない"
artifact_requirements:
  - "contract_readiness_check.py JSON output"
  - "pytest output"
```

