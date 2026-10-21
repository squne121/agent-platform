## Verification Commands

```bash
# AC - rg -F pattern string containing "python3" should not be flagged as legacy python
$ rg -F "^python3 " .claude/skills/issue-refinement-loop/tests/test_vc_scope.py
$ rg -n "VC_LEGACY_PYTHON3" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/fixtures/`（fixture 追加のみ）
