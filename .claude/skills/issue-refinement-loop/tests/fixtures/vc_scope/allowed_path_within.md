## Verification Commands

```bash
# AC - path within allowed paths (fixtures subdirectory)
$ uv run pytest .claude/skills/issue-refinement-loop/tests/fixtures/vc_scope/sample.md -v
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/fixtures/`（fixture 追加のみ）
