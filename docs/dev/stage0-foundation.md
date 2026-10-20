# Stage 0: identity / toolchain / pytest / CI foundation (I-3)

Parent: #5. Child: #6. Source baseline: loop-protocol tag `pre-split-agent-platform-20261020`
(commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`). See `refinement-migration-manifest.json`.

## Identity
- The trusted slug lives only in `config/repository_identity.v1.json`, read by
  `scripts/agent-guards/repo_identity.py`. No env var or CLI argument can override it.
- Missing, malformed, wrong-schema, extra-key or invalid-slug config raises
  `RepositoryIdentityError` (fail closed). Matching is exact.
- `github_credentialless_read.py` resolves the slug through it at call time.

## Checks
- `scripts/ci/check_no_functional_slug.py ROOT...`: exit 0 clean / 1 violation / 2 bad root or nothing scanned.
  A line carrying `# allow-legacy-slug` is skipped (used only by the checker and its own fixtures). Python is scanned by AST (string constants and `+` concatenations of them, so comments are ignored; matching is case-insensitive); json/yaml/toml/sh as text;
  Markdown is exempt. The words `squne121` and `LOOP_PROTOCOL` alone are not violations; only the
  legacy slug and `refs/` namespace are. Absolute home paths stay with `scripts/check_public_hygiene.sh`.
- `scripts/ci/run_stage0_tests.py` (CI job `stage0-closure`): collect must exit 0, per-file counts must equal
  `stage0-test-manifest.json`, the run must pass with 0 skipped. Update the manifest when tests change.
- pytest runs with `--import-mode=importlib --strict-markers` and `xfail_strict`.

## Not done here
Everything under `deferred` in the migration manifest, including the runtime dependencies
(`pyyaml`, `jsonschema`, `referencing`), which are added with their first consumer in Stage 1a.
