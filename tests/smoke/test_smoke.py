import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_public_hygiene.sh"


def run_hygiene(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo)], check=False, capture_output=True, text=True
    )


def git_repo(path: Path, content: str) -> Path:
    (path / "a.txt").write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    return path


def test_claude_md_imports_agents_md():
    assert (ROOT / "CLAUDE.md").read_text().splitlines() == ["@AGENTS.md"]
    assert (ROOT / "AGENTS.md").stat().st_size > 0


def test_hygiene_passes_on_clean_repo():
    assert run_hygiene(ROOT).returncode == 0


def test_hygiene_passes_on_clean_fixture(tmp_path):
    assert run_hygiene(git_repo(tmp_path, "hello\n")).returncode == 0


def test_hygiene_fails_on_home_path(tmp_path):
    forbidden = "/home/" + "x/secret\n"
    assert run_hygiene(git_repo(tmp_path, forbidden)).returncode == 1


def test_hygiene_errors_when_nothing_checked(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert run_hygiene(tmp_path).returncode == 2


def test_hygiene_detects_anthropic_key_and_json_session_id(tmp_path):
    key = "sk-ant-" + "api03-abcdefghijklmnopqrstuvwx\n"
    assert run_hygiene(git_repo(tmp_path, key)).returncode == 1


def test_hygiene_detects_json_session_id(tmp_path):
    sid = '"session' + '_id": "abc"\n'
    assert run_hygiene(git_repo(tmp_path, sid)).returncode == 1


def test_hygiene_errors_when_only_binary_files(tmp_path):
    (tmp_path / "bin").write_bytes(b"\0\1\2")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "bin"], cwd=tmp_path, check=True)
    assert run_hygiene(tmp_path).returncode == 2


def test_hygiene_ignores_kebab_case_words_ending_in_sk(tmp_path):
    assert (
        run_hygiene(git_repo(tmp_path, "see task-management-and-scheduling-system\n")).returncode
        == 0
    )
