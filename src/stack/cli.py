"""The `stack` command: the four projects as one system.

I1: `stack pins` (compare the pinned commits with the local checkouts, or check them on
GitHub), `stack pins --update`. Later steps add up / status / down and the loop.
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
