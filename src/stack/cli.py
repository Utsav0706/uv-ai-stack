"""The `stack` command: the four projects as one system.

- `stack pins`: the pinned commits next to the local checkouts (`--remote`: on GitHub;
  `--update`: move them);
- `stack up` / `status` / `down`: the whole system on this machine;
- `stack config`: a project's config for the combined system (for docker-compose.yml).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import httpx
import typer

from stack import __version__
from stack import pins as pins_mod

app = typer.Typer(help="Scout, Gearbox, Dyno and Pitwall as one system.", no_args_is_help=True)


@app.callback()
def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


@app.command()
def version() -> None:
    """Print the stack's version."""
    typer.echo(f"uv-ai-stack {__version__}")


@app.command()
def pins(
    update: Annotated[
        bool, typer.Option("--update", help="Move every pin to the local checkout's commit.")
    ] = False,
    remote: Annotated[
        bool, typer.Option("--remote", help="Check every pinned commit exists on GitHub.")
    ] = False,
    stack_file: Annotated[Path, typer.Option(help="The pins file.")] = pins_mod.STACK_FILE,
) -> None:
    """Show the four pinned commits next to the local checkouts."""
    projects = pins_mod.load(stack_file)
    root = stack_file.resolve().parent
    if remote:
        headers = {"Accept": "application/vnd.github+json"}
        if token := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        missing = []
        with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20) as c:
            for p in projects:
                ok = pins_mod.on_github(p, c)
                typer.echo(f"{'ok ' if ok else '-- '} {p.repo:<22} {p.commit[:12]}")
                if not ok:
                    missing.append(p.name)
        if missing:
            typer.echo(f"pinned commits not on GitHub: {', '.join(missing)}", err=True)
            raise typer.Exit(1)
        return

    states = [pins_mod.local_state(p, root) for p in projects]
    for s in states:
        head = s.head[:12] if s.head else "-"
        flag = " (uncommitted changes)" if s.dirty else ""
        typer.echo(f"{s.name:<8} pin {s.pinned[:12]}  local {head}  {s.relation}{flag}")
    if update:
        moves = {s.name: s.head for s in states if s.head and s.relation != "same"}
        dirty = [s.name for s in states if s.dirty and s.name in moves]
        if dirty:
            typer.echo(f"refusing: uncommitted changes in {', '.join(dirty)}", err=True)
            raise typer.Exit(1)
        if not moves:
            typer.echo("all pins already match the checkouts")
            return
        changed = pins_mod.update(stack_file, moves)  # type: ignore[arg-type]
        typer.echo(f"moved pins: {', '.join(changed)} (review and commit stack.toml)")


def _setup(scout_via_gearbox: bool = False):  # type: ignore[no-untyped-def]
    from stack import services as svc

    root = Path.cwd()
    projects = {p.name: p for p in pins_mod.load(root / pins_mod.STACK_FILE)}
    return root, projects, svc.services(root, scout_via_gearbox)


@app.command()
def up(
    ui: Annotated[
        bool, typer.Option("--ui", help="Also the Streamlit apps (~200 MB each).")
    ] = False,
    scout_via_gearbox: Annotated[
        bool, typer.Option("--scout-via-gearbox", help="Scout's model calls go through Gearbox.")
    ] = False,
) -> None:
    """Start the system: MLflow, Pitwall, Gearbox (routed by Pitwall), Scout,
    Prometheus, Grafana (and the dashboards with --ui). Run from the uv-ai-stack folder."""
    from stack import configs, runner
    from stack import services as svc

    root, projects, all_services = _setup(scout_via_gearbox)
    for state in (pins_mod.local_state(p, root) for p in projects.values()):
        if state.relation != "same" or state.dirty:
            note = " with uncommitted changes" if state.dirty else ""
            typer.echo(f"note: {state.name} is {state.relation} of its pin{note}")
    if not runner.healthy(svc.OLLAMA_URL):
        typer.echo(
            "error: Ollama isn't answering on 127.0.0.1:11434 (Pitwall and tier S need it)",
            err=True,
        )
        raise typer.Exit(1)

    gearbox_dir = (root / projects["gearbox"].path).resolve()
    configs.write(
        gearbox_dir / "config.yaml",
        root / svc.GEARBOX_CONFIG,
        configs.gearbox_via_pitwall,
        "Gearbox routed by Pitwall",
        "http://127.0.0.1:8003",
        10,
    )
    if scout_via_gearbox:
        scout_dir = (root / projects["scout"].path).resolve()
        configs.write(
            scout_dir / "config.yaml",
            root / svc.SCOUT_CONFIG,
            configs.scout_via_gearbox,
            "Scout through Gearbox",
            "http://127.0.0.1:8002",
        )

    ordered = svc.start_order(svc.selected(all_services, ui))
    commands = {s.name: svc.command(s, projects, root) for s in ordered}
    outcomes = runner.up(root, ordered, commands, typer.echo)
    failed = [o for o in outcomes if o.action == "failed"]
    if failed or len(outcomes) < len(ordered):
        raise typer.Exit(1)
    typer.echo(
        f"\nall {len(outcomes)} services healthy; `stack status` for memory, `stack down` to stop"
    )


@app.command()
def status() -> None:
    """Health, process and memory of every service."""
    from stack import runner
    from stack import services as svc

    root, _projects, all_services = _setup()
    pids = runner.read_pids(root)
    total = 0.0
    typer.echo(f"{'service':<18} {'port':>5}  {'health':<8} {'pid':>7}  {'MB':>6}  started by")
    for s in all_services:
        ok = runner.healthy(s.health)
        pid = pids.get(s.name) if s.name in pids else (runner.pid_on_port(s.port) if ok else None)
        mb = runner.memory_mb(pid) if ok else None
        total += mb or 0
        owner = "stack" if s.name in pids else ("other" if ok else "-")
        typer.echo(
            f"{s.name:<18} {s.port:>5}  {'ok' if ok else 'down':<8} {pid or '-':>7}  "
            f"{mb if mb is not None else '-':>6}  {owner}"
        )
    ollama = "ok" if runner.healthy(svc.OLLAMA_URL) else "down"
    typer.echo(f"{'ollama':<18} {11434:>5}  {ollama:<8} (a system service; not started by stack)")
    typer.echo(f"memory of the running services: {total:.0f} MB")


@app.command()
def down() -> None:
    """Stop the processes `stack up` started (only those)."""
    from stack import runner

    n = runner.down(Path.cwd(), typer.echo)
    typer.echo(f"{n} processes stopped" if n else "nothing started by stack is running")


@app.command("config")
def config_cmd(
    project: Annotated[
        str, typer.Argument(help="gearbox (routed by Pitwall) or scout (via Gearbox)")
    ],
    out: Annotated[Path, typer.Option(help="Where to write it.")],
    pitwall_url: Annotated[str, typer.Option(help="For gearbox.")] = "http://127.0.0.1:8003",
    gearbox_url: Annotated[str, typer.Option(help="For scout.")] = "http://127.0.0.1:8002",
    canary_percent: Annotated[float, typer.Option(help="For gearbox.")] = 10,
) -> None:
    """Write a project's config for the combined system (used by docker-compose.yml)."""
    from stack import configs

    root, projects, _ = _setup()
    source = (root / projects[project].path).resolve() / "config.yaml"
    if project == "gearbox":
        configs.write(
            source,
            out,
            configs.gearbox_via_pitwall,
            "Gearbox routed by Pitwall",
            pitwall_url,
            canary_percent,
        )
    elif project == "scout":
        configs.write(source, out, configs.scout_via_gearbox, "Scout through Gearbox", gearbox_url)
    else:
        typer.echo("error: project must be gearbox or scout", err=True)
        raise typer.Exit(1)
    typer.echo(f"wrote {out}")
