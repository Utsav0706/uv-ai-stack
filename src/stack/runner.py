"""Start, check and stop the services (`stack up / status / down`).

- up: in dependency order; a service already healthy on its port is reused, not
  started twice; each new one starts detached, logs to runs/logs/<name>.log, and must
  pass its health check before anything that depends on it starts.
- down: stops only the processes `stack up` started (their ids are in runs/pids.json),
  with their child processes; never anything else on those ports.
- status: health, process id and memory for every service.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stack.services import RUNS, Service

PIDS = RUNS / "pids.json"
LOGS = RUNS / "logs"


def healthy(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def read_pids(root: Path) -> dict[str, int]:
    path = root / PIDS
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_pids(root: Path, pids: dict[str, int]) -> None:
    path = root / PIDS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pids, indent=2) + "\n", encoding="utf-8")


def start_process(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> int:
    """Start detached (it outlives `stack up`); returns the process id."""
    log.parent.mkdir(parents=True, exist_ok=True)
    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **env}
    with log.open("ab") as out:
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=full_env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=full_env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    return proc.pid


def stop_process(pid: int) -> bool:
    """Stop a process and its children; False if it was already gone."""
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True
        )
        return result.returncode == 0
    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def memory_mb(pid: int | None) -> float | None:
    """Resident memory of a process and all its children, in MB."""
    if pid is None:
        return None
    import psutil

    try:
        proc = psutil.Process(pid)
        procs = [proc, *proc.children(recursive=True)]
        return round(sum(p.memory_info().rss for p in procs) / 1e6, 0)
    except psutil.Error:
        return None


def pid_on_port(port: int) -> int | None:
    """The process listening on a port (to show memory for services not started by us)."""
    import psutil

    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except psutil.Error:
        return None
    return None


@dataclass
class Outcome:
    name: str
    action: str  # started | reused | failed
    detail: str = ""


def up(
    root: Path,
    ordered: list[Service],
    commands: dict[str, tuple[list[str], Path]],
    echo: Callable[[str], None],
    *,
    start: Callable[[list[str], Path, dict[str, str], Path], int] = start_process,
    is_healthy: Callable[[str], bool] = healthy,
    is_open: Callable[[int], bool] = port_open,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[Outcome]:
    pids = read_pids(root)
    outcomes: list[Outcome] = []
    for s in ordered:
        if is_healthy(s.health):
            outcomes.append(Outcome(s.name, "reused", "already healthy"))
            echo(f"reuse  {s.name:<18} :{s.port} already healthy")
            continue
        if is_open(s.port):
            outcomes.append(Outcome(s.name, "failed", f"port {s.port} in use, not healthy"))
            echo(f"FAIL   {s.name:<18} :{s.port} is taken by something that isn't healthy")
            break
        cmd, cwd = commands[s.name]
        if not Path(cmd[0]).exists():
            outcomes.append(Outcome(s.name, "failed", f"{cmd[0]} not found"))
            echo(f"FAIL   {s.name:<18} {cmd[0]} not found (create that project's .venv)")
            break
        pid = start(cmd, cwd, s.env, root / LOGS / f"{s.name}.log")
        pids[s.name] = pid
        write_pids(root, pids)
        echo(f"start  {s.name:<18} :{s.port} pid {pid}, waiting for {s.health}")
        deadline = clock() + s.timeout_s
        while not is_healthy(s.health):
            if clock() > deadline:
                break
            sleep(2)
        if is_healthy(s.health):
            outcomes.append(Outcome(s.name, "started", f"pid {pid}"))
            echo(f"ok     {s.name:<18} healthy")
        else:
            outcomes.append(Outcome(s.name, "failed", f"not healthy after {s.timeout_s:.0f}s"))
            echo(
                f"FAIL   {s.name:<18} not healthy after {s.timeout_s:.0f}s: see {LOGS / s.name}.log"
            )
            break
    return outcomes


def down(
    root: Path, echo: Callable[[str], None], stop: Callable[[int], bool] = stop_process
) -> int:
    pids = read_pids(root)
    for name, pid in reversed(list(pids.items())):
        echo(f"{'stopped' if stop(pid) else 'gone   '} {name:<18} pid {pid}")
    write_pids(root, {})
    return len(pids)
