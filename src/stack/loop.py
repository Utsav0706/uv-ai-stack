"""`stack loop`: the loop through all four projects, against the system `stack up` started.

1. Route   - prompts go through Gearbox, which asks Pitwall for the tier (router v4, 10%
             canary to the candidate); capped at tier S, so answering is free.
2. Watch   - Pitwall's drift check on the prompts its production model just routed.
3. Gate    - Pitwall's promotion gate (Dyno on 119 cases through the running service,
             under-routing, p95): a dry run by default, so nothing in the registry moves.
4. Report  - drift, the gate's verdict, and the alerts Prometheus has firing (step 1
             already printed which router and tier answered each request).

With --promote, step 3 becomes the real release: a canary replay through Gearbox
(PAID, capped), its report in MLflow, then the gate, which moves the production alias
on a pass. Steps run in order and stop on the first unexpected result.

Everything goes through the projects' own interfaces: HTTP to Gearbox and Prometheus,
Pitwall's command line in Pitwall's virtual environment, and Pitwall's prompt files.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

GEARBOX = "http://127.0.0.1:8002"
PROMETHEUS = "http://127.0.0.1:9090"
PRODUCTION_ROUTER = "v4-finetuned"  # Gearbox's name for Pitwall's production model
MIN_DRIFT_PROMPTS = 30  # Pitwall's drift check refuses fewer
SOURCES = ("test", "pool_new")  # Pitwall's data/derived files: general prompts, drifted ones

Echo = Callable[[str], None]
RunCli = Callable[[list[str]], tuple[int, str]]


class LoopStopped(Exception):
    """A step's result wasn't one the loop can carry on from."""


@dataclass
class Traffic:
    sent: int = 0
    ok: int = 0
    routers: Counter = field(default_factory=Counter)  # x-gearbox-router of each answer
    tiers: Counter = field(default_factory=Counter)


@dataclass
class LoopResult:
    traffic: Traffic
    drift: bool | None  # None: too few prompts reached Pitwall to check
    gate: str  # "would promote", "promoted", "rejected" or "nothing to release"
    alerts: list[str]
    canary_rows: str | None = None


def prompts(pitwall_dir: Path, source: str, n: int, seed: int) -> list[str]:
    """The user prompts from one of Pitwall's prompt files, in a seeded order (repeating
    if there are fewer than n)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    path = pitwall_dir / "data" / "derived" / f"{source}.jsonl"
    texts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            texts.append(next(m["content"] for m in record["messages"] if m["role"] == "user"))
    if not texts:
        raise LoopStopped(f"no prompts in {path}")
    random.Random(seed).shuffle(texts)
    return [texts[i % len(texts)] for i in range(n)]


def send(client: httpx.Client, texts: list[str], echo: Echo) -> Traffic:
    """Each prompt through Gearbox, capped at the free local tier."""
    traffic = Traffic()
    headers = {"x-gearbox-max-tier": "S", "x-gearbox-cascade": "off"}
    for i, text in enumerate(texts, 1):
        body = {"model": "auto", "messages": [{"role": "user", "content": text}], "max_tokens": 64}
        r = client.post("/v1/chat/completions", json=body, headers=headers)
        traffic.sent += 1
        if r.status_code == 200:
            traffic.ok += 1
            traffic.routers[r.headers.get("x-gearbox-router", "?")] += 1
            traffic.tiers[r.headers.get("x-gearbox-tier", "?")] += 1
        if i % 10 == 0 or i == len(texts):
            echo(f"  {i}/{len(texts)} sent, {traffic.ok} answered")
    return traffic


def firing_alerts(client: httpx.Client) -> list[str]:
    try:
        alerts = client.get(f"{PROMETHEUS}/api/v1/alerts").json()["data"]["alerts"]
    except (httpx.HTTPError, ValueError, KeyError):
        return ["(Prometheus not reachable)"]
    return sorted({a["labels"]["alertname"] for a in alerts if a.get("state") == "firing"})


def pitwall_cli(pitwall_dir: Path, exe: Path) -> RunCli:
    """Run `pitwall ...` in Pitwall's folder and venv; returns (exit code, output)."""

    def run(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            [str(exe), *args],
            cwd=pitwall_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "MLFLOW_DISABLE_AGENT_HINT": "1"},
        )
        return proc.returncode, proc.stdout + proc.stderr

    return run


NOISE = (
    "mlflow.agent.hint",
    "Loading weights",
    "View run ",
    "View experiment ",
    "unauthenticated requests to the HF Hub",
)


def _show(echo: Echo, output: str) -> None:
    for line in output.strip().splitlines():
        if line.strip() and not any(n in line for n in NOISE):
            echo(f"  | {line}")


def run_loop(
    *,
    texts: list[str],
    client: httpx.Client,
    pitwall: RunCli,
    echo: Echo,
    promote: bool = False,
    canary_n: int = 40,
    cap_gbp: float = 0.05,
    gate_ref: str = "candidate",
) -> LoopResult:
    n = len(texts)
    echo(f"1. Route: {n} prompts through Gearbox, which asks Pitwall (tier capped at S: free)")
    traffic = send(client, texts, echo)
    if traffic.ok < n:
        raise LoopStopped(f"only {traffic.ok}/{n} requests answered; see Gearbox's log")
    if not any(r.startswith("v4") for r in traffic.routers):
        raise LoopStopped(
            f"Gearbox didn't route through Pitwall (routers: {dict(traffic.routers)})"
        )
    echo(f"  routers: {dict(traffic.routers)}  tiers: {dict(traffic.tiers)}")

    # The drift check reads production's routing log: answers from Gearbox's cache never
    # reached Pitwall, and canary requests were routed by the candidate model.
    routed = traffic.routers.get(PRODUCTION_ROUTER, 0)
    drift: bool | None = None
    if routed < MIN_DRIFT_PROMPTS:
        echo(
            f"\n2. Watch: skipped, production routed {routed} of these (the rest were the "
            f"canary or Gearbox's cache); the drift check needs {MIN_DRIFT_PROMPTS}"
        )
    else:
        echo(f"\n2. Watch: Pitwall's drift check on the {routed} prompts production routed")
        code, out = pitwall(["drift", "check", "--window", str(routed)])
        _show(echo, out)
        if code not in (0, 3):
            raise LoopStopped(f"drift check failed (exit {code})")
        drift = code == 3

    canary_rows = None
    if promote:
        echo(f"\n3a. Canary: {canary_n} labelled prompts through Gearbox (PAID, cap GBP {cap_gbp})")
        code, out = pitwall(["canary", "replay", "--n", str(canary_n), "--cap-gbp", str(cap_gbp)])
        _show(echo, out)
        found = re.search(r"(runs[\\/]canary-\S+\.jsonl)", out)
        if code != 0 or not found:
            raise LoopStopped(f"canary replay failed (exit {code})")
        canary_rows = found.group(1)
        code, out = pitwall(["canary", "report", canary_rows])
        _show(echo, out)
        if code != 0:
            raise LoopStopped(f"canary report failed (exit {code})")
        echo("\n3b. Gate and release: Dyno's gate; moves the production alias on a pass")
        args = ["promote", gate_ref]
    else:
        echo(f"\n3. Gate (dry run): Dyno's gate on {gate_ref}; nothing in the registry moves")
        args = ["promote", gate_ref, "--dry-run"]
    code, out = pitwall(args)
    _show(echo, out)
    if "already production" in out:
        gate = "nothing to release"  # the candidate is what production already serves
    elif "REJECTED" in out:
        gate = "rejected"
    elif code == 0:
        gate = "promoted" if promote else "would promote"
    else:
        raise LoopStopped(f"the gate couldn't run (exit {code})")

    echo("\n4. Report")
    alerts = firing_alerts(client)
    shown = {True: "DRIFT", False: "none", None: "not checked"}[drift]
    echo(f"  drift: {shown}   gate: {gate}")
    echo(f"  alerts firing: {', '.join(alerts) if alerts else 'none'}")
    return LoopResult(traffic, drift, gate, alerts, canary_rows)
