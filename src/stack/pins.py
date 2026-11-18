"""The pinned commits of the four projects (stack.toml), checked locally and on GitHub.

Each repository moves on its own; the system is a specific combination of four
commits that were tested together. A pin moves only on purpose (`stack pins
--update`), so any result quoted for the system can be rebuilt from those commits.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

STACK_FILE = Path("stack.toml")
SHA = r"^[0-9a-f]{40}$"


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")  # owner/name on GitHub
    path: Path  # local checkout, relative to stack.toml
    commit: str = Field(pattern=SHA)


def load(stack_file: Path = STACK_FILE) -> list[Project]:
    data = tomllib.loads(stack_file.read_text(encoding="utf-8"))
    projects = [Project.model_validate(p) for p in data.get("project", [])]
    names = [p.name for p in projects]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate project names in {stack_file}")
    return projects


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@dataclass(frozen=True)
class LocalState:
    name: str
    pinned: str
    head: str | None  # None: no checkout
    dirty: bool
    relation: str  # same | ahead | behind | diverged | missing | unknown


def local_state(project: Project, root: Path) -> LocalState:
    path = (root / project.path).resolve()
    head = _git(path, "rev-parse", "HEAD")
    if head.returncode != 0:
        return LocalState(project.name, project.commit, None, False, "missing")
    sha = head.stdout.strip()
    dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip())
    if sha == project.commit:
        relation = "same"
    elif _git(path, "merge-base", "--is-ancestor", project.commit, sha).returncode == 0:
        relation = "ahead"  # the checkout has commits after the pin
    elif _git(path, "merge-base", "--is-ancestor", sha, project.commit).returncode == 0:
        relation = "behind"
    elif _git(path, "cat-file", "-e", f"{project.commit}^{{commit}}").returncode == 0:
        relation = "diverged"
    else:
        relation = "unknown"  # the pinned commit isn't in this checkout (fetch first)
    return LocalState(project.name, project.commit, sha, dirty, relation)


def on_github(project: Project, client: httpx.Client) -> bool:
    """True if the pinned commit exists in the project's GitHub repository."""
    r = client.get(f"/repos/{project.repo}/commits/{project.commit}")
    return r.status_code == 200


def update(stack_file: Path, new_commits: dict[str, str]) -> list[str]:
    """Rewrite the commit line of each named project; returns the names changed.
    A text edit, not a TOML re-dump, so the comments in stack.toml stay."""
    text = stack_file.read_text(encoding="utf-8")
    changed = []
    for name, sha in new_commits.items():
        if not re.fullmatch(SHA, sha):
            raise ValueError(f"{name}: {sha!r} is not a full commit id")
        # From this project's `name =` line, lazily, to its own `commit = "..."` line.
        start = rf'(\[\[project\]\]\s*\nname = "{re.escape(name)}"\n'
        pattern = re.compile(start + r'(?:.*\n)*?commit = ")([0-9a-f]{40})(")')
        text, n = pattern.subn(rf"\g<1>{sha}\g<3>", text, count=1)
        if n == 0:
            raise ValueError(f"no project named {name!r} in {stack_file}")
        changed.append(name)
    stack_file.write_text(text, encoding="utf-8", newline="\n")
    return changed
