import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_marketplace_pins_stable_plugin_by_sha():
    mp = load(".claude-plugin/marketplace.json")
    assert mp["name"] == "ap-stable"
    (entry,) = mp["plugins"]
    assert entry["name"] == "ap-pilot"
    assert "version" not in entry
    src = entry["source"]
    assert src["source"] == "git-subdir"
    assert src["url"].startswith("https://")
    assert re.fullmatch(r"[0-9a-f]{40}", src["sha"])


def test_experimental_is_not_distributed_and_has_distinct_name():
    mp = load(".claude-plugin/marketplace.json")
    stable = load("plugins/ap-pilot/.claude-plugin/plugin.json")
    exp = load("plugins/ap-pilot-exp/.claude-plugin/plugin.json")
    assert stable["name"] != exp["name"]
    assert exp["name"] not in {p["name"] for p in mp["plugins"]}
    assert (ROOT / "plugins/ap-pilot/skills/ap-ping/SKILL.md").is_file()
