from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from stack import pins
from stack.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def make_repo(path: Path, commits: int = 2) -> list[str]:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    shas = []
    for i in range(commits):
        (path / "f.txt").write_text(str(i))
        git(path, "add", "f.txt")
        git(path, "commit", "-q", "-m", f"c{i}")
        shas.append(git(path, "rev-parse", "HEAD"))
    return shas


def stack_file(tmp: Path, entries: dict[str, str]) -> Path:
    text = "# pins\n"
    for name, sha in entries.items():
        text += (
            f'\n[[project]]\nname = "{name}"\nrole = "r"\nrepo = "o/{name}"\n'
            f'path = "{name}"\ncommit = "{sha}"\n'
        )
    path = tmp / "stack.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_pins_file_is_valid() -> None:
    projects = pins.load(ROOT / "stack.toml")
    assert [p.name for p in projects] == ["scout", "gearbox", "dyno", "pitwall"]


def test_relations_to_the_pin(tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a")  # pinned at its first commit, checkout ahead
    b = make_repo(tmp_path / "b")  # pinned at HEAD
    c = make_repo(tmp_path / "c")  # checkout moved back behind the pin
    git(tmp_path / "c", "checkout", "-q", c[0])
    path = stack_file(tmp_path, {"a": a[0], "b": b[1], "c": c[1], "gone": "0" * 40})
    states = {p.name: pins.local_state(p, tmp_path) for p in pins.load(path)}
    assert states["a"].relation == "ahead"
    assert states["b"].relation == "same"
    assert states["c"].relation == "behind"
    assert states["gone"].relation == "missing"


def test_uncommitted_changes_are_flagged_and_block_update(tmp_path: Path) -> None:
    shas = make_repo(tmp_path / "a")
    (tmp_path / "a" / "f.txt").write_text("edited")
    path = stack_file(tmp_path, {"a": shas[0]})
    assert pins.local_state(pins.load(path)[0], tmp_path).dirty
    result = runner.invoke(app, ["pins", "--update", "--stack-file", str(path)])
    assert result.exit_code == 1 and "uncommitted" in result.output


def test_update_moves_pins_and_keeps_comments(tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a")
    b = make_repo(tmp_path / "b")
    path = stack_file(tmp_path, {"a": a[0], "b": b[1]})
    result = runner.invoke(app, ["pins", "--update", "--stack-file", str(path)])
    assert result.exit_code == 0, result.output
    assert "moved pins: a" in result.output
    loaded = {p.name: p.commit for p in pins.load(path)}
    assert loaded == {"a": a[1], "b": b[1]}
    assert path.read_text(encoding="utf-8").startswith("# pins")


def test_update_rejects_bad_input(tmp_path: Path) -> None:
    path = stack_file(tmp_path, {"a": "a" * 40})
    with pytest.raises(ValueError, match="full commit id"):
        pins.update(path, {"a": "abc"})
    with pytest.raises(ValueError, match="no project named"):
        pins.update(path, {"zzz": "b" * 40})


def test_github_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.path.endswith("a" * 40) else 422)

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    good = pins.Project(name="x", role="r", repo="o/x", path=Path("x"), commit="a" * 40)
    bad = good.model_copy(update={"commit": "b" * 40})
    assert pins.on_github(good, client) and not pins.on_github(bad, client)


def test_version_command() -> None:
    assert "uv-ai-stack" in runner.invoke(app, ["version"]).output
