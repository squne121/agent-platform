"""I-4-2 (#66): GitHub rewrites control characters when persisting a comment.

Live finding (see docs/dev/i4-2-snapshot-publisher-manifest.json): U+0001..U+001F (except
tab/LF/VT/FF/CR/BS) and U+0080..U+009F are stored in caret notation, so a body that embeds
them raw never matches its own readback hash. The comment builder must not emit them raw.
"""

import importlib.util
import json
import re
from pathlib import Path

_ECS = Path(__file__).resolve().parents[1] / "scripts" / "ensure_contract_snapshot.py"
_spec = importlib.util.spec_from_file_location("ecs_transport_controls_under_test", _ECS)
assert _spec is not None and _spec.loader is not None
ecs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecs)

# characters GitHub rewrites on persist (measured live) plus DEL, which is kept but unsafe to rely on
_REWRITTEN = re.compile("[\x01-\x08\x0e-\x1f\x80-\x9f]")
HOSTILE = "bell\x07 esc\x1b[2K c1\x85\x9b del\x7f 日本語 🚀 back\\slash \"q\" 'sq'"


def _review_result() -> dict:
    return {
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": {"blocked_reasons": [HOSTILE]},
            "vc_preflight": "pass",
            "declared_path_overlap": {"note": HOSTILE},
        },
        "vc_preflight_classifications": [{"stdout_head": [HOSTILE], "raw_command": HOSTILE}],
    }


def test_comment_body_has_no_characters_github_would_rewrite():
    body = ecs._build_contract_review_comment(
        67,
        "squne121/agent-platform",
        _review_result(),
        "<!-- marker -->",
        "sha256:" + "0" * 64,
        {"issue_number": 67, "note": HOSTILE},
    )
    assert not _REWRITTEN.search(body)
    assert "\x7f" not in body


def test_json_compact_is_json_equivalent():
    value = {"a": [HOSTILE, "é", " "], "b": {"c": "\x85"}}
    encoded = ecs._json_compact(value)
    assert json.loads(encoded) == value
    assert not re.search("[\x7f-\x9f]", encoded)
