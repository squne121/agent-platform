## Verification Commands

```bash
# AC - uv run is allowed (not legacy python3)
$ uv run python3 .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py --help
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -v
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -v
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`（新規）
- `.claude/skills/issue-refinement-loop/tests/fixtures/`（fixture 追加のみ）
