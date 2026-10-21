## Verification Commands

```bash
# Fix 3: compound command with bare python3 after &&
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py && python3 .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
