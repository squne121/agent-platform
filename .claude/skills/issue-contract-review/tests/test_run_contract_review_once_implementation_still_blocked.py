"""
tests/test_run_contract_review_once_implementation_still_blocked.py

Issue #1914 AC4（回帰）:

`## Verification Commands` セクションを持たない `issue_kind: implementation` の
Issue body に対して `run_contract_review_once.py` を実行した場合、Issue #1914 の
delivery-rollup parent 向け適用除外（AC3）を追加した後も、従来通り
`status: blocked` のままであることを固定する。`issue_kind: implementation` は
`_is_delivery_rollup_parent_without_vc_section()` の対象外であり、Step 5
（`baseline_vc_preflight.py`）は引き続き実行され、VC セクション欠如による
`VC001_NO_VERIFICATION_COMMANDS_SECTION` を含む `blocked` を返す。

Runtime Verification Applicability: not_applicable
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once

_ISSUE_NUMBER = 1234
_REPO = "squne121/agent-platform"


# ---------------------------------------------------------------------------
# Fixture body: issue_kind: implementation, no `## Verification Commands`
# ---------------------------------------------------------------------------

_IMPLEMENTATION_BODY_NO_VC = textwrap.dedent(
    """\
    ## Machine-Readable Contract

    ```yaml
    contract_schema_version: v1
    issue_kind: implementation
    parent_issue: "none"
    ```

    ## Outcome

    fixture implementation issue（VC セクションなし、回帰テスト用）。

    ## Acceptance Criteria

    - [ ] AC1: fixture

    ## Allowed Paths

    - some/fixture/path.md

    ## Stop Conditions

    - N/A（fixture）
    """
)


def _make_readiness_go_json() -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "go",
        "body_sha256": "sha256:abc",
        "source_checks": [],
        "errors": [],
        "minimal_context": [],
        "fix_hint": None,
    }


def _make_product_spec_not_applicable_json() -> dict:
    return {
        "schema": "product_spec_check/v1",
        "applicability": "not_applicable",
        "decision": "pass",
        "triggers": {},
        "conditions": {},
        "blocked_reasons": [],
        "body_sha256": "sha256:abc",
        "source_provenance": {
            "source_type": "github_issue_body",
            "body_file": None,
        },
    }


def _make_vc_preflight_blocked_json() -> dict:
    return {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "status": "blocked",
        "results": [],
        "errors": [
            {
                "rule": "VC001_NO_VERIFICATION_COMMANDS_SECTION",
                "message": "Verification Commands section not found",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Regression: delivery-rollup detector must not fire for implementation kind
# ---------------------------------------------------------------------------


class TestImplementationKindDetectorRegression:
    """AC4: `_is_delivery_rollup_parent_without_vc_section` は
    issue_kind: implementation を対象外とする（適用除外の対象は
    parent_mode: delivery-rollup のみ）。"""

    def test_detector_false_for_implementation_kind(self):
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(
            _IMPLEMENTATION_BODY_NO_VC
        )


class TestImplementationKindStillBlockedBehavior:
    """AC4: Step 5（baseline_vc_preflight.py）は implementation kind に対して
    引き続き実行され、VC001 起因の blocked を返す。"""

    def test_vc_preflight_still_invoked_and_blocked(self):
        readiness_json = _make_readiness_go_json()
        product_spec_json = _make_product_spec_not_applicable_json()
        vc_blocked_json = _make_vc_preflight_blocked_json()

        run_script_results = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_blocked_json, 0, None),
            ]
        )

        def run_script(*args, **kwargs):
            return next(run_script_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=run_script):
            with patch.object(
                _rcr_mod, "_run_shell_script", return_value=(0, "OK: no blockers", "")
            ):
                with patch.object(
                    _rcr_mod, "check_existing_go_comment", return_value=(None, None)
                ):
                    with patch.object(
                        _rcr_mod,
                        "fetch_body_from_github",
                        return_value=(_IMPLEMENTATION_BODY_NO_VC, None),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "vc_preflight"
        assert result["checks"]["vc_preflight"] == "blocked"
        assert any(
            err.get("rule") == "VC001_NO_VERIFICATION_COMMANDS_SECTION"
            for err in result["vc_preflight_classifications"] + []
            if isinstance(err, dict)
        ) or result["current_vc_result"] == vc_blocked_json


# ---------------------------------------------------------------------------
# Subprocess integration test: end-to-end still blocked (not silently "go")
# ---------------------------------------------------------------------------

_FAKE_GH_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Fake `gh` for offline subprocess integration testing (Issue #1914 AC4).
    set -euo pipefail

    BODY_FILE="${FAKE_GH_BODY_FILE:?FAKE_GH_BODY_FILE not set}"

    if [[ "${1:-}" == "issue" && "${2:-}" == "view" ]]; then
      shift 2
      json_fields=""
      jq_expr=""
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --json) json_fields="$2"; shift 2 ;;
          --jq) jq_expr="$2"; shift 2 ;;
          --repo) shift 2 ;;
          *) shift ;;
        esac
      done
      if [[ "$jq_expr" == ".body" ]]; then
        cat "$BODY_FILE"
        exit 0
      fi
      if [[ "$jq_expr" == ".state" ]]; then
        echo "OPEN"
        exit 0
      fi
      python3 - "$json_fields" "$BODY_FILE" <<'PYEOF'
    import json
    import sys

    fields = sys.argv[1].split(",")
    with open(sys.argv[2], "r", encoding="utf-8") as f:
        body = f.read()
    out = {}
    for field in fields:
        if field == "body":
            out["body"] = body
        elif field == "title":
            out["title"] = "実装: fixture (regression)"
        elif field == "labels":
            out["labels"] = []
        elif field == "state":
            out["state"] = "OPEN"
        elif field == "comments":
            out["comments"] = []
        else:
            out[field] = None
    print(json.dumps(out))
    PYEOF
      exit 0
    fi

    if [[ "${1:-}" == "api" ]]; then
      echo "[]"
      exit 0
    fi

    exit 1
    """
)


class TestImplementationKindSubprocessRegression:
    """AC4 / Issue #1914 Notes (c): 実プロセスとして起動しても
    implementation issue_kind + VC セクションなしは `status: go` に
    ならない（依然 blocked のまま）ことを確認する。"""

    def test_subprocess_invocation_does_not_return_go(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            fake_gh_path = tmp_path / "gh"
            fake_gh_path.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
            fake_gh_path.chmod(fake_gh_path.stat().st_mode | stat.S_IEXEC)

            body_file = tmp_path / "issue_body.md"
            body_file.write_text(_IMPLEMENTATION_BODY_NO_VC, encoding="utf-8")

            env = dict(os.environ)
            env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
            env["FAKE_GH_BODY_FILE"] = str(body_file)
            env["GH_BIN"] = str(fake_gh_path)

            proc = subprocess.run(
                [
                    sys.executable,
                    str(_RCR_PATH),
                    "--issue-number",
                    str(_ISSUE_NUMBER),
                    "--repo",
                    _REPO,
                    "--mode",
                    "static",
                    "--skip-idempotency-check",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )

            payload = json.loads(proc.stdout)

        # exit 0 is reserved for status: go (see module docstring's exit code
        # table). A VC-section-less implementation issue must not exit 0.
        assert proc.returncode != 0, (
            f"implementation issue_kind without a Verification Commands section "
            f"must not resolve to status: go; stdout={proc.stdout!r}"
        )
        assert payload["status"] == "blocked"
        assert payload["status"] != "go"
