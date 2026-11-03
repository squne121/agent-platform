"""
tests/test_run_contract_review_once_delivery_rollup_skip.py

Issue #1914 AC3:

`run_contract_review_once.py` を、`## Verification Commands` セクションを持たない
delivery-rollup parent の Issue body（`issue_kind: parent` / `parent_mode:
delivery-rollup`）に対して `--mode static` で実行すると、`baseline_vc_preflight.py`
を呼ばずに `status` が `blocked` ではなく `go` になることを検証する。

- `TestDeliveryRollupSkipBehavior`: `_run_script` / `fetch_body_from_github` を
  patch した behavior test。baseline_vc_preflight 用の `_run_script` 呼び出しが
  発生しないこと（呼び出し回数）と `status: go` を確認する。
- `TestDeliveryRollupSkipSubprocess`: `run_contract_review_once.py` を実際に
  subprocess として起動する統合テスト（Issue #1914 Notes (c)）。`gh` を fake
  実行ファイルへ差し替え、ネットワーク・外部認証なしで完結させる。

Runtime Verification Applicability: not_applicable
（本 Issue の Runtime Verification Applicability セクション参照。静的検証・
pytest 回帰テストで検証が完結する）
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import module under test (既存テストと同じ importlib ベースのロード方式)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once

_ISSUE_NUMBER = 1890
_REPO = "squne121/agent-platform"


# ---------------------------------------------------------------------------
# Fixture body: delivery-rollup parent, no `## Verification Commands` section
# ---------------------------------------------------------------------------

_DELIVERY_ROLLUP_PARENT_BODY = textwrap.dedent(
    """\
    ## Machine-Readable Contract

    ```yaml
    contract_schema_version: v1
    issue_kind: parent
    parent_mode: delivery-rollup
    closure_mode: child-complete
    ```

    ## Summary

    調査・整理系の delivery-rollup parent Issue（fixture）。

    ## Goal

    child issue 群の完了を束ねる。

    ## Desired Destination

    全 child issue が完了した状態。

    ## Current Validated Scope

    - child issue A
    - child issue B

    ## Decisions Fixed

    - closure_mode: child-complete

    ## Quality Decision Record

    - Status: `child-complete`

    ## Parent Closure Rule

    全 child issue が CLOSED になったら本 parent を CLOSE する。

    ## Child Issues

    - #1（仮）

    ## Remaining Parent Gaps

    なし

    ## Phase Handoff Contract

    N/A（単独 delivery-rollup parent）

    ## Acceptance Criteria

    - [ ] AC1: 全 child issue が CLOSED である
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


# ---------------------------------------------------------------------------
# Behavior test (mocked _run_script boundary)
# ---------------------------------------------------------------------------


class TestDeliveryRollupSkipBehavior:
    """AC3: delivery-rollup parent（VC セクションなし）で baseline_vc_preflight
    の _run_script 呼び出しが発生せず、status: go になる。"""

    def test_baseline_vc_preflight_not_invoked_and_status_go(self):
        readiness_json = _make_readiness_go_json()
        product_spec_json = _make_product_spec_not_applicable_json()

        # Only 2 _run_script calls expected: readiness, product_spec.
        # A 3rd call would mean baseline_vc_preflight.py was (incorrectly)
        # invoked for this delivery-rollup-without-VC-section body.
        run_script_results = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
            ]
        )

        def run_script(*args, **kwargs):
            try:
                return next(run_script_results)
            except StopIteration as exc:  # pragma: no cover - failure path
                raise AssertionError(
                    "baseline_vc_preflight.py must not be invoked for a "
                    "delivery-rollup parent without a Verification Commands section"
                ) from exc

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
                        return_value=(_DELIVERY_ROLLUP_PARENT_BODY, None),
                    ):
                        with patch.object(
                            _rcr_mod,
                            "_run_declared_path_overlap_check",
                            return_value={
                                "schema": "declared_path_overlap/v1",
                                "advisory": True,
                                "blocking": False,
                                "decision": "unavailable",
                                "disjoint": None,
                                "overlapping_prs": [],
                                "inventory": None,
                                "errors": [],
                            },
                        ):
                            result = run_once(
                                _ISSUE_NUMBER, _REPO, skip_idempotency_check=True
                            )

        assert result["status"] == "go"
        assert result["checks"]["vc_preflight"] == "not_applicable"
        assert result["vc_preflight_status"] == "not_applicable"
        assert result["source"] == "delivery_rollup_parent_without_verification_commands"

    def test_delivery_rollup_detector_true_for_fixture_body(self):
        """`_is_delivery_rollup_parent_without_vc_section` がこの fixture body を
        正しく True と判定することを固定する（回帰・drift 検出用）。"""
        assert _rcr_mod._is_delivery_rollup_parent_without_vc_section(
            _DELIVERY_ROLLUP_PARENT_BODY
        )

    def test_delivery_rollup_detector_false_when_vc_section_present(self):
        """delivery-rollup parent でも `## Verification Commands` セクションが
        存在する場合はスキップしない（PR #1878 P1 review と同じ設計方針）。"""
        body_with_vc = _DELIVERY_ROLLUP_PARENT_BODY + textwrap.dedent(
            """

            ## Verification Commands

            ```bash
            # AC1
            $ echo ok
            ```
            """
        )
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body_with_vc)


# ---------------------------------------------------------------------------
# Subprocess integration test (Issue #1914 Notes (c))
# ---------------------------------------------------------------------------

_FAKE_GH_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Fake `gh` for offline subprocess integration testing (Issue #1914).
    # No network / auth required. Only understands the subset of `gh`
    # invocations exercised by run_contract_review_once.py's call graph.
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
            out["title"] = "delivery-rollup fixture parent"
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
      # Native dependency API (blocked_by) and any other `gh api` call
      # (e.g. declared_path_overlap's GraphQL query, advisory-only):
      # respond with an empty JSON array. Callers that need object shape
      # (GraphQL) treat this as an unusable payload and degrade to their
      # own advisory-unavailable fallback rather than crashing.
      echo "[]"
      exit 0
    fi

    exit 1
    """
)


class TestDeliveryRollupSkipSubprocess:
    """AC3 / Issue #1914 Notes (c): run_contract_review_once.py を実プロセスとして
    起動し、delivery-rollup parent（VC セクションなし）で status: go を返すことを
    確認する（gh は fake 実行ファイルに差し替え、ネットワーク・認証は不要）。"""

    def test_subprocess_invocation_returns_go_for_delivery_rollup_parent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            fake_gh_path = tmp_path / "gh"
            fake_gh_path.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
            fake_gh_path.chmod(fake_gh_path.stat().st_mode | stat.S_IEXEC)

            body_file = tmp_path / "issue_body.md"
            body_file.write_text(_DELIVERY_ROLLUP_PARENT_BODY, encoding="utf-8")

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

            assert proc.returncode == 0, (
                f"expected exit 0 (status: go); "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )

            import json

            payload = json.loads(proc.stdout)

        assert payload["schema"] == "CONTRACT_REVIEW_ONCE_RESULT_V1"
        assert payload["status"] == "go"
        assert payload["source"] == "delivery_rollup_parent_without_verification_commands"
        assert payload["checks"]["vc_preflight"] == "not_applicable"
