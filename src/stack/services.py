"""The services that make up the running system, and how each is started on Windows.

Every service is started by its own project's script, from its own repository and
virtual environment: the stack only orchestrates, it never imports a project's code.
Ollama is not here: it runs as a system service, and `stack up` only checks it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from stack.pins import Project

RUNS = Path("runs")
GEARBOX_CONFIG = RUNS / "gearbox-stack.yaml"
SCOUT_CONFIG = RUNS / "scout-via-gearbox.yaml"
OLLAMA_URL = "http://127.0.0.1:11434/api/version"


@dataclass(frozen=True)
class Service:
    name: str
    project: str  # which repository it belongs to
    args: list[str]  # after the project's python (or the full command for `exe` ones)
    port: int
    health: str
    depends_on: tuple[str, ...] = ()
    ui: bool = False  # Streamlit apps: ~200 MB each, only with `stack up --ui`
    exe: str | None = None  # a console script in the project's venv instead of python
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 180.0


def venv_bin(project_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return project_dir / ".venv" / "Scripts" / f"{name}.exe"
    return project_dir / ".venv" / "bin" / name


def services(root: Path, scout_via_gearbox: bool = False) -> list[Service]:
    """The stack's services in a valid start order. `root` is the uv-ai-stack folder."""
    scout_env = {"CONFIG_PATH": str((root / SCOUT_CONFIG).resolve())} if scout_via_gearbox else {}
    return [
        Service(
            "mlflow", "pitwall", ["scripts/mlflow_server.py"], 5000, "http://127.0.0.1:5000/health"
        ),
        Service(
            "pitwall",
            "pitwall",
            ["scripts/dev_api.py"],
            8003,
            "http://127.0.0.1:8003/health",
            depends_on=("mlflow",),
        ),
        Service(
            "gearbox",
            "gearbox",
            ["scripts/dev_api.py"],
            8002,
            "http://127.0.0.1:8002/health",
            depends_on=("pitwall",),
            env={"CONFIG_PATH": str((root / GEARBOX_CONFIG).resolve())},
        ),
        Service(
            "scout",
            "scout",
            ["scripts/dev_api.py"],
            8001,
            "http://127.0.0.1:8001/health",
            depends_on=("gearbox",) if scout_via_gearbox else (),
            env=scout_env,
        ),
        Service(
            "prometheus",
            "pitwall",
            ["scripts/monitoring.py", "prometheus"],
            9090,
            "http://127.0.0.1:9090/-/ready",
        ),
        Service(
            "grafana",
            "pitwall",
            ["scripts/monitoring.py", "grafana"],
            3000,
            "http://127.0.0.1:3000/api/health",
            depends_on=("prometheus",),
        ),
        Service(
            "scout-ui",
            "scout",
            ["scripts/dev_ui.py"],
            8501,
            "http://127.0.0.1:8501/_stcore/health",
            depends_on=("scout",),
            ui=True,
        ),
        Service(
            "gearbox-dashboard",
            "gearbox",
            ["scripts/dev_dashboard.py"],
            8502,
            "http://127.0.0.1:8502/_stcore/health",
            depends_on=("gearbox",),
            ui=True,
        ),
        Service(
            "dyno-dashboard",
            "dyno",
            ["dashboard", "--port", "8503"],
            8503,
            "http://127.0.0.1:8503/_stcore/health",
            ui=True,
            exe="dyno",
        ),
    ]


def selected(all_services: list[Service], ui: bool) -> list[Service]:
    return [s for s in all_services if ui or not s.ui]


def start_order(chosen: list[Service]) -> list[Service]:
    """Dependencies first; refuses a dependency that isn't selected or a cycle."""
    by_name = {s.name: s for s in chosen}
    ordered: list[Service] = []
    visiting: set[str] = set()

    def visit(s: Service) -> None:
        if s in ordered:
            return
        if s.name in visiting:
            raise ValueError(f"dependency cycle at {s.name}")
        visiting.add(s.name)
        for dep in s.depends_on:
            if dep not in by_name:
                raise ValueError(f"{s.name} needs {dep}, which is not selected")
            visit(by_name[dep])
        visiting.discard(s.name)
        ordered.append(s)

    for s in chosen:
        visit(s)
    return ordered


def command(service: Service, projects: dict[str, Project], root: Path) -> tuple[list[str], Path]:
    """(command line, working folder) for a service, from its project's checkout."""
    project_dir = (root / projects[service.project].path).resolve()
    if service.exe:
        exe = venv_bin(project_dir, service.exe)
        return [str(exe), *service.args], project_dir
    python = venv_bin(project_dir, "python")
    return [str(python), *service.args], project_dir
