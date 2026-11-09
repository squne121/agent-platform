"""Unit tests for get_ci_failed_log.py using mocked gh CLI."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent
    / "scripts"
    / "get_ci_failed_log.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("get_ci_failed_log", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_module()

HEAD_SHA = "abc123def456abc123def456abc123def456"


def make_run(
    head_sha=HEAD_SHA,
    status="completed",
    conclusion="failure",
    workflow="CI",
    run_id=1001,
    attempt=1,
):
    return {
        "databaseId": run_id,
        "attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "headSha": head_sha,
        "workflowName": workflow,
        "event": "push",
        "createdAt": "2026-06-13T00:00:00Z",
        "updatedAt": "2026-06-13T00:01:00Z",
        "url": "https://github.com/example/repo/actions/runs/1001",
    }


def make_args(head_sha=HEAD_SHA, workflow=None, max_bytes=60000, run_id=None):
    import argparse
    return argparse.Namespace(
        repo="owner/repo", pr=1, head_sha=head_sha,
        workflow=workflow, max_bytes=max_bytes, run_id=run_id,
    )


def parse_marker(capsys) -> dict:
    captured = capsys.readouterr()
    for line in captured.out.splitlines():
        if line.startswith("CI_FAILED_LOG_RESULT_V1_JSON:"):
            return json.loads(line[len("CI_FAILED_LOG_RESULT_V1_JSON:"):].strip())
    raise AssertionError(f"CI_FAILED_LOG_RESULT_V1_JSON not found in stdout:\n{captured.out}")


# ---------------------------------------------------------------------------
# AC2: no matching run
# ---------------------------------------------------------------------------
class TestNoMatchingRun:
    def test_empty_run_list(self, monkeypatch, capsys):
        monkeypatch.setattr(mod, "run_gh", lambda *a: (0, "[]", ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "no_matching_run"

    def test_sha_mismatch(self, monkeypatch, capsys):
        runs = [make_run(head_sha="different_sha")]
        monkeypatch.setattr(mod, "run_gh", lambda *a: (0, json.dumps(runs), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "no_matching_run"


# ---------------------------------------------------------------------------
# Error classification: auth_error / gh_error / malformed separate from no_matching_run
# ---------------------------------------------------------------------------
class TestErrorClassification:
    def test_auth_error(self, monkeypatch, capsys):
        monkeypatch.setattr(mod, "run_gh", lambda *a: (1, "", "HTTP 401: Requires authentication"))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        m = parse_marker(capsys)
        assert m["status"] == "auth_error"

    def test_gh_error_generic(self, monkeypatch, capsys):
        monkeypatch.setattr(mod, "run_gh", lambda *a: (1, "", "network error"))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        m = parse_marker(capsys)
        assert m["status"] == "gh_error"

    def test_malformed_json(self, monkeypatch, capsys):
        monkeypatch.setattr(mod, "run_gh", lambda *a: (0, "not valid json", ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        m = parse_marker(capsys)
        assert m["status"] == "malformed_gh_response"


# ---------------------------------------------------------------------------
# AC3: pending
# ---------------------------------------------------------------------------
class TestPending:
    @pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
    def test_pending_statuses(self, monkeypatch, capsys, status):
        run = make_run(status=status, conclusion=None)
        monkeypatch.setattr(mod, "run_gh", lambda *a: (0, json.dumps([run]), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        m = parse_marker(capsys)
        assert m["status"] == "ci_pending"


# ---------------------------------------------------------------------------
# AC4: success
# ---------------------------------------------------------------------------
class TestSuccess:
    def test_ci_passed_marker(self, monkeypatch, capsys):
        run = make_run(status="completed", conclusion="success")
        monkeypatch.setattr(mod, "run_gh", lambda *a: (0, json.dumps([run]), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        m = parse_marker(capsys)
        assert m["status"] == "ci_passed"


# ---------------------------------------------------------------------------
# timed_out / cancelled / action_required are treated as failed
# ---------------------------------------------------------------------------
class TestFailedConclusions:
    @pytest.mark.parametrize("conclusion", ["timed_out", "cancelled", "action_required"])
    def test_failed_conclusions(self, monkeypatch, capsys, conclusion):
        run = make_run(status="completed", conclusion=conclusion)
        list_out = json.dumps([run])
        jobs_out = json.dumps({"id": 9, "name": "build", "conclusion": conclusion})
        log_out = "some log"

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "log-failed" in joined:
                return (0, log_out, "")
            if "--paginate" in joined:
                return (0, jobs_out, "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "ci_failed"


# ---------------------------------------------------------------------------
# AC5: failure primary — failed_jobs populated in marker
# ---------------------------------------------------------------------------
class TestFailurePrimary:
    def test_primary_log_and_failed_jobs_in_marker(self, monkeypatch, capsys):
        run = make_run(conclusion="failure")
        list_out = json.dumps([run])
        log_out = "##[error] step failed\nsome log line"
        jobs_json = json.dumps({"id": 9, "name": "build", "conclusion": "failure"})

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, jobs_json, "")
            if "log-failed" in joined:
                return (0, log_out, "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "ci_failed"
        assert m["retrieval_method"] == "gh_log_failed"
        assert len(m["failed_jobs"]) > 0
        assert m["failed_jobs"][0]["name"] == "build"


# ---------------------------------------------------------------------------
# AC6: fallback
# ---------------------------------------------------------------------------
class TestFallback:
    def test_fallback_success_with_failed_jobs(self, monkeypatch, capsys):
        run = make_run(conclusion="failure")
        list_out = json.dumps([run])
        jobs_out = json.dumps({"id": 9, "name": "build", "conclusion": "failure"})
        job_log = "Error in build step\nFailed"

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, jobs_out, "")
            if "log-failed" in joined:
                return (0, "", "")
            if "jobs/" in joined and "logs" in joined:
                return (0, job_log, "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "ci_failed"
        assert m["retrieval_method"] == "rest_job_logs"
        assert len(m["failed_jobs"]) > 0

    def test_fallback_failure_returns_log_unavailable(self, monkeypatch, capsys):
        run = make_run(conclusion="failure")
        list_out = json.dumps([run])
        jobs_out = json.dumps({"id": 9, "name": "build", "conclusion": "failure"})

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, jobs_out, "")
            return (1, "", "error")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "log_unavailable"


# ---------------------------------------------------------------------------
# Multiple attempts — latest selected
# ---------------------------------------------------------------------------
class TestMultipleAttempts:
    def test_latest_attempt_selected(self, monkeypatch, capsys):
        runs = [
            make_run(conclusion="failure", attempt=1, run_id=1001),
            make_run(conclusion="failure", attempt=2, run_id=1001),
        ]
        list_out = json.dumps(runs)
        log_out = "latest attempt log"
        calls = []

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, "", "")
            if "log-failed" in joined:
                calls.append(joined)
                if "--attempt=2" in joined:
                    return (0, log_out, "")
                return (1, "", "wrong attempt")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["attempt"] == 2


# ---------------------------------------------------------------------------
# Ambiguous run (multiple workflows, no filter)
# ---------------------------------------------------------------------------
class TestAmbiguousRun:
    def test_ambiguous_returns_ambiguous_run(self, monkeypatch, capsys):
        runs = [
            make_run(conclusion="failure", workflow="CI"),
            make_run(conclusion="failure", workflow="Deploy"),
        ]
        monkeypatch.setattr(mod, "run_gh", lambda *a: (0, json.dumps(runs), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "ambiguous_run"

    def test_workflow_exact_match_resolves_ambiguity(self, monkeypatch, capsys):
        runs = [
            make_run(conclusion="failure", workflow="CI"),
            make_run(conclusion="failure", workflow="Deploy"),
        ]
        list_out = json.dumps(runs)
        jobs_out = json.dumps({"id": 9, "name": "typecheck", "conclusion": "failure"})

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, jobs_out, "")
            if "log-failed" in joined:
                return (0, "some log", "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args",
                            lambda *a, **k: make_args(workflow="CI"))
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        m = parse_marker(capsys)
        assert m["status"] == "ci_failed"
        assert m["workflow_name"] == "CI"


# ---------------------------------------------------------------------------
# AC8: redaction / truncation / ANSI
# ---------------------------------------------------------------------------
class TestRedaction:
    def test_token_redacted(self):
        text = "token: ghp_" + "A" * 36
        redacted, applied = mod.redact_tokens(text)
        assert "[REDACTED]" in redacted
        assert applied is True

    @pytest.mark.parametrize(
        "token",
        [
            "ghp_" + "A" * 36,
            "ghs_" + "A" * 36,
            "github_pat_" + "A" * 59,
            "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature_123",
        ],
        ids=["classic_ghp", "classic_ghs", "github_pat", "bare_jwt"],
    )
    def test_existing_token_formats_fully_redacted(self, token):
        """AC3 (#2728): classic ghp_/ghs_/github_pat_/bare-JWT 形式の
        redact_tokens() 非回帰を named parametrize node で明示的に検証する。"""
        redacted, applied = mod.redact_tokens(f"before {token} after")
        assert applied is True
        assert redacted == "before [REDACTED] after"
        assert token not in redacted

    def test_sha_not_redacted(self):
        sha = "abc123def456abc123def456abc123def456abc1"
        text = f"head sha: {sha}"
        redacted, applied = mod.redact_tokens(text)
        assert sha in redacted
        assert applied is False

    def test_truncation(self):
        text = "x" * 200
        result, truncated = mod.truncate(text, 100)
        assert truncated is True
        assert "[TRUNCATED]" in result

    def test_ansi_stripped(self):
        text = "\x1b[31mred\x1b[0m"
        assert mod.strip_ansi(text) == "red"

    def test_redact_tokens_return_type(self):
        result = mod.redact_tokens("clean text")
        assert isinstance(result, tuple) and len(result) == 2
        text, flag = result
        assert isinstance(text, str)
        assert isinstance(flag, bool)

    def test_new_installation_token_fully_redacted(self):
        """GIVEN a deterministic synthetic new-format ghs_<APP_ID>_<JWT>
        installation token (not a real credential; PR #2735 / #2746 / #2751
        precedent fixture shape: 'ghs_' + APP_ID + '_' + JWT header.payload.signature,
        with exactly two '.' separators and both '_' and '-' characters present,
        padded to installation-token scale) WHEN embedded in 'before <token> after'
        and passed to redact_tokens() THEN the result is exactly
        'before [REDACTED] after' with no token fragment (APP_ID, header, payload,
        or signature) remaining anywhere in the output.
        """
        jwt_header = "eyJhbGciOiJSUzI1NiJ9"
        jwt_payload = "eyJpc3MiOiJodHRwczovL2V4YW1wbGUuY29tIn0"
        jwt_signature = "s1gN4tuRe-_placeholder123456789"
        app_id = "123456789"
        token = f"ghs_{app_id}_{jwt_header}.{jwt_payload}.{jwt_signature}"
        while len(token) < 520:
            jwt_payload += "A"
            jwt_signature += "B"
            token = f"ghs_{app_id}_{jwt_header}.{jwt_payload}.{jwt_signature}"

        # Fixture sanity: JWT-shaped, exactly two '.' separators, contains
        # both '_' and '-', and is at real installation-token scale so a
        # matcher that lost the '._-' charset extension cannot pass by luck.
        assert token.count(".") == 2
        assert "_" in token
        assert "-" in token
        assert len(token) >= 520
        assert token.startswith(f"ghs_{app_id}_")

        text = f"before {token} after"
        redacted, applied = mod.redact_tokens(text)

        assert applied is True
        assert redacted == "before [REDACTED] after"
        assert app_id not in redacted
        assert jwt_header not in redacted
        assert jwt_payload not in redacted
        assert jwt_signature not in redacted
        assert token not in redacted


# ---------------------------------------------------------------------------
# marker JSON validity
# ---------------------------------------------------------------------------
class TestMarkerValidity:
    def test_marker_has_required_fields(self, monkeypatch, capsys):
        run = make_run(conclusion="failure", workflow="CI: build & test")
        list_out = json.dumps([run])
        jobs_out = json.dumps({"id": 9, "name": "build", "conclusion": "failure"})

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, jobs_out, "")
            if "log-failed" in joined:
                return (0, "log text", "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit):
            mod.main()
        m = parse_marker(capsys)
        required_fields = {
            "schema", "status", "run_id", "attempt", "head_sha",
            "workflow_name", "failed_jobs", "retrieval_method",
            "redaction_applied", "truncated",
        }
        assert required_fields.issubset(m.keys())
        assert m["schema"] == "CI_FAILED_LOG_RESULT_V1"

    def test_workflow_name_with_special_chars_is_valid_json(self, monkeypatch, capsys):
        run = make_run(conclusion="failure", workflow='CI: build "test" & deploy')
        list_out = json.dumps([run])

        def fake_run_gh(*a):
            joined = " ".join(str(x) for x in a)
            if "list" in joined:
                return (0, list_out, "")
            if "--paginate" in joined:
                return (0, "", "")
            if "log-failed" in joined:
                return (0, "log", "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: make_args())
        with pytest.raises(SystemExit):
            mod.main()
        # parse_marker asserts JSON is valid; special chars must not break it
        m = parse_marker(capsys)
        assert 'CI: build "test"' in m["workflow_name"]
