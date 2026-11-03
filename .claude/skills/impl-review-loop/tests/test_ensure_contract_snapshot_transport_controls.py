"""I-4-2 (#66): the posted snapshot body must survive the `gh api` readback unchanged.

Measured with gh 2.102.0 against live GitHub: GitHub stores the body verbatim, but `gh api`
sanitizes the JSON it prints. Raw U+0001-0007 / U+000E-001F / U+0080-009F and the escape text
``\\u00XX`` (XX in 01-08, 0c, 0e-1f, 80-9f; any number of preceding backslashes) become caret
notation; ``\\u005c`` spelling, tab/LF/VT/FF/CR/BS and DEL pass. ``json.loads(dumps(v)) == v`` says
nothing about that, so these tests model the sanitizer on the received JSON text and require the
producer body to be a fixed point of it.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "ecs_transport_controls_under_test", _SCRIPTS / "ensure_contract_snapshot.py"
)
assert _spec is not None and _spec.loader is not None
ecs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecs)

_PARSER = Path(__file__).resolve().parents[2] / "issue-contract-review" / "scripts"
_pspec = importlib.util.spec_from_file_location(
    "parser_transport_controls_under_test", _PARSER / "contract_review_result_parser.py"
)
assert _pspec is not None and _pspec.loader is not None
parser = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(parser)

REPO = "squne121/agent-platform"
ISSUE = 67
COMMENT_ID = 6600001
ISSUE_URL = f"https://github.com/{REPO}/issues/{ISSUE}"
BODY_SHA = "sha256:" + "0" * 64
_CARET_RAW = {*range(0x01, 0x08), *range(0x0E, 0x20), *range(0x80, 0xA0)}
_CARET_ESC = {*range(0x01, 0x09), 0x0C, *range(0x0E, 0x20), *range(0x80, 0xA0)}
_ESC_RE = re.compile(r"\\u00([0-9a-fA-F]{2})")


def gh_response_sanitizer(json_text: str) -> str:
    """Model of the gh JSON-response sanitizer, fitted to the live probe."""

    def caret(code: int) -> str:
        return "^" + chr((code & 0x7F) ^ 0x40)

    out: list[str] = []
    i = 0
    while i < len(json_text):
        ch = json_text[i]
        if ch == "\\":
            m = _ESC_RE.match(json_text, i)
            if m is not None and int(m.group(1), 16) in _CARET_ESC:
                preceding = 0
                while preceding < i and json_text[i - preceding - 1] == "\\":
                    preceding += 1
                if preceding % 2 == 1:
                    out.append("\\")
                out.append(caret(int(m.group(1), 16)))
                i += 6
                continue
        out.append(caret(ord(ch)) if ord(ch) in _CARET_RAW else ch)
        i += 1
    return "".join(out)


def gh_readback(stored_body: str) -> str:
    return json.loads(gh_response_sanitizer(json.dumps({"body": stored_body}, ensure_ascii=False)))["body"]


def build(classifications, *, overlap=None, spec=None, fingerprint=None) -> str:
    result = {
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": spec if spec is not None else {"decision": "pass"},
            "vc_preflight": "pass",
            "declared_path_overlap": overlap if overlap is not None else {"advisory": True},
        },
        "vc_preflight_classifications": classifications,
    }
    fp = fingerprint or {
        "issue_number": ISSUE,
        "contract_source_kind": "issue_comment",
        "contract_source_id": str(COMMENT_ID),
        "contract_body_sha256": BODY_SHA,
        "allowed_paths_normalized_sha256": "b" * 64,
        "base_ref": "main",
        "base_sha_at_snapshot": "c" * 40,
    }
    return ecs._build_contract_review_comment(ISSUE, REPO, result, "<!-- marker -->", BODY_SHA, fp)


def parse_received(body: str) -> dict:
    comment = {
        "id": COMMENT_ID,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
        "html_url": f"{ISSUE_URL}#issuecomment-{COMMENT_ID}",
        "user": {"login": "squne121", "id": 63350259, "type": "User"},
        "author_association": "OWNER",
        "body": gh_readback(body),
    }
    parsed = parser.parse_contract_review_results([comment], ISSUE_URL)
    assert len(parsed) == 1 and parsed[0]["status"] == "go", parsed
    assert parser.is_fingerprint_ready_go(parsed[0]["inner"], COMMENT_ID, ISSUE)
    return parsed[0]["inner"]


# Strings with no real control character: must round-trip exactly, in any field.
LOSSLESS = [
    "\\u001b",
    "\\u0085",
    "\\u001B[2K",
    "\\\\u001b",
    "\\\\\\u001b",
    "\\\\\\\\u0085",
    "path\\foo\\bar\\n\\u0008",
    "\\u005cu001b",
    "\\x1b \\^[ \\^E",
    "del\x7f nbsp  ls  日本語 🚀 \"q\" 'sq' tab\t lf\n vt\x0b bs\b ff\f cr\r",
    "'; DROP -- ''",
]


def test_gh_sanitizer_model_matches_live_probe():
    # raw C0/C1 and literal escapes are rewritten; BS/FF/DEL raw and \ pass
    assert gh_response_sanitizer('"a\x1bb"') == '"a^[b"'
    assert gh_response_sanitizer('"a\\u001bb"') == '"a^[b"'
    assert json.loads(gh_response_sanitizer(json.dumps("a\\u001bb"))) == "a\\^[b"
    assert gh_response_sanitizer('"a\\u0008b\\u000cb"') == '"a^Hb^Lb"'
    assert gh_response_sanitizer('"a\x08\x0c\x7fb\\u005cu001b"') == '"a\x08\x0c\x7fb\\u005cu001b"'
    assert gh_response_sanitizer('"\\u0009\\u000a\\u000b\\u000d"') == '"\\u0009\\u000a\\u000b\\u000d"'


@pytest.mark.parametrize("value", LOSSLESS)
def test_json_compact_is_gh_fixed_point_and_json_equivalent(value):
    for payload in (value, [value], {"k": value, "n": {"v": [value]}}, {value: 1}):
        encoded = ecs._json_compact(payload)
        assert json.loads(encoded) == payload
        assert gh_readback(encoded) == encoded


def test_naive_dumps_is_not_a_gh_fixed_point():
    # guards the model: the failure this fix addresses is real, not a tautology
    assert gh_readback(json.dumps("\\u001b")) != json.dumps("\\u001b")
    assert gh_readback("a\x85b") != "a\x85b"


@pytest.mark.parametrize("value", LOSSLESS)
def test_authority_bearing_fields_round_trip_exactly_through_gh_and_parser(value):
    classifications = [
        {
            "ac": "AC1",
            "decision": "pass",
            "raw_command": f"echo {value}",
            "command_hash": "sha256:" + "a" * 64,
            "execution_key_hash": "sha256:" + "d" * 64,
            "nested": {"x": [value, {"y": value}]},
            "stdout_head": [value],
        }
    ]
    body = build(
        classifications,
        overlap={"note": value},
        spec={"decision": "pass", "blocked_reasons": [value]},
    )
    assert gh_readback(body) == body
    assert body.encode("utf-8") == gh_readback(body).encode("utf-8")
    inner = parse_received(body)
    got = inner["checks"]["vc_preflight"]["classifications"]
    assert got == classifications
    assert got[0]["raw_command"] == f"echo {value}"
    assert inner["checks"]["declared_path_overlap"] == {"note": value}
    assert inner["checks"]["product_spec_check"]["blocked_reasons"] == [value]


@pytest.mark.parametrize("ch", [chr(c) for c in sorted(_CARET_RAW | {0})])
def test_real_control_in_authority_bearing_field_fails_closed(ch):
    with pytest.raises(ValueError, match="unrepresentable"):
        build([{"ac": "AC1", "raw_command": f"echo {ch}"}])
    with pytest.raises(ValueError):
        build([{"ac": "AC1"}], overlap={"note": ch})
    with pytest.raises(ValueError):
        build([{"ac": "AC1"}], spec={"note": ch})
    with pytest.raises(ValueError):
        build([{"ac": "AC1", ch: 1}])


def test_real_control_in_verbose_field_is_shown_as_stable_literal_text():
    verbose = "bell\x07 esc\x1b[2K c1\x85\x9b lit\\u001b \\\\u0085"
    classifications = [
        {
            "ac": "AC1",
            "raw_command": "echo ok",
            "stdout_head": [verbose],
            "stderr_head": [verbose],
            "runner_env_delta": {"K\x1b": verbose},
            "pnpm_gate_evidence": {"e": [verbose]},
        }
    ]
    body = build(classifications)
    assert gh_readback(body) == body
    got = parse_received(body)["checks"]["vc_preflight"]["classifications"][0]
    shown = "bell\\x07 esc\\x1b[2K c1\\x85\\x9b lit\\u001b \\\\u0085"
    assert got["raw_command"] == "echo ok"
    assert got["stdout_head"] == [shown] and got["stderr_head"] == [shown]
    assert got["runner_env_delta"] == {"K\\x1b": shown}
    assert got["pnpm_gate_evidence"] == {"e": [shown]}


def test_verbose_key_collision_after_conversion_is_rejected():
    with pytest.raises(ValueError, match="collision"):
        build([{"ac": "AC1", "runner_env_delta": {"\x1b": 1, "\\x1b": 2}}])


def test_posted_body_bytes_equal_readback_bytes_for_hostile_mix():
    classifications = [
        {
            "ac": "AC1",
            "raw_command": "printf '\\u001b' && echo \\\\u0085",
            "command_hash": "sha256:" + "e" * 64,
            "stdout_head": ["\x1b\\u001b\x85"],
        }
    ]
    body = build(classifications)
    assert gh_readback(body).encode() == body.encode()
    assert len(body.encode()) == len(gh_readback(body).encode())


def test_size_is_measured_on_the_final_transport_representation():
    # Original JSON is under the cap, but literal \u001b grows (\\u001b -> \u001b).
    verbose = ["\\u001b" * 100 for _ in range(17)]
    classifications = [{"ac": "AC1", "decision": "pass", "stdout_head": verbose}]
    original = json.dumps(classifications, ensure_ascii=False, separators=(",", ":"))
    assert len(original) <= ecs._MAX_CLASSIFICATIONS_TRANSPORT_CHARS
    assert len(ecs._json_compact_measure(classifications)) > ecs._MAX_CLASSIFICATIONS_TRANSPORT_CHARS
    out, meta = ecs.compact_vc_preflight_classifications(classifications)
    assert meta["compact"] is True and meta["truncated"] is True
    assert out == [{"ac": "AC1", "decision": "pass"}]
    assert meta["original_payload_sha256"] == ecs.sha256_of(original)
    assert len(ecs._json_compact_measure(out)) <= ecs._MAX_CLASSIFICATIONS_TRANSPORT_CHARS
    body = build(classifications)
    assert gh_readback(body) == body
    assert parse_received(body)["checks"]["vc_preflight"]["classifications"] == out


def test_non_verbose_overflow_after_normalization_is_rejected_not_truncated():
    big = [{"ac": "AC1", "raw_command": "\\u001b" * 1700}]
    assert len(json.dumps(big, ensure_ascii=False, separators=(",", ":"))) <= 16_000
    with pytest.raises(ValueError, match="too_large"):
        ecs.compact_vc_preflight_classifications(big)


def test_unrepresentable_value_stops_before_any_post(monkeypatch):
    calls = []
    monkeypatch.setattr(ecs, "post_comment", lambda *a, **k: calls.append(a) or (None, "posted", None))
    with pytest.raises(ValueError):
        build([{"ac": "AC1", "raw_command": "x\x1by"}])
    assert calls == []


def test_nested_key_with_real_control_in_authority_field_fails_closed():
    with pytest.raises(ValueError, match="unrepresentable"):
        build([{"ac": "AC1", "nested": {"\x1b": 1}}])


def test_lone_surrogate_fails_closed_as_value_error():
    with pytest.raises(ValueError):
        build([{"ac": "AC1", "raw_command": "a\ud800"}])


def test_unbounded_advisory_values_stop_before_post_not_after():
    # parser caps one JSON collection at 16384 chars / 32 deep; GitHub caps a comment body
    with pytest.raises(ValueError, match="too_large"):
        build([{"ac": "AC1"}], overlap={"p": "'" * 20_000})
    with pytest.raises(ValueError, match="too_large"):
        build([{"ac": "AC1"}], spec={"decision": "pass", "s": "x" * 30_000})
    deep: dict = {}
    cur = deep
    for _ in range(40):
        cur["d"] = {}
        cur = cur["d"]
    with pytest.raises(ValueError, match="too_deep"):
        build([{"ac": "AC1", "pnpm_gate_evidence": deep}])
    with pytest.raises(ValueError, match="too_deep"):
        build([{"ac": "AC1", "raw_command": "x"}], overlap=deep)
