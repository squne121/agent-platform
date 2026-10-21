## Verification Commands

```bash
# AC - all clean commands
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -v
$ rg -n "VC_LEGACY_PYTHON3" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
$ rg -n "VC_SCOPE_BROAD_SEARCH_PATH|VC_SCOPE_OUTSIDE_ALLOWED_PATH" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
$ rg -n "prose|VC_PROSE_REFERENCE_ONLY" .claude/skills/issue-refinement-loop/tests/test_vc_scope.py
$ rg -n "ARTIFACT" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/fixtures/`（fixture 追加のみ）
