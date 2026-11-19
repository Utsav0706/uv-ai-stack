"""Smoke test for the whole system in Docker Compose, run by CI.

    python3 scripts/system_smoke.py

CI has no model keys and no Ollama, so no answer can be produced; what is checked is
that the services are up and wired to each other:

- Scout, Gearbox, Pitwall and MLflow answer their health checks;
- a request to Gearbox goes through its router v4 to Pitwall: Gearbox records it under
  router v4-finetuned, and Pitwall records the request it got (no model is registered
  in a fresh MLflow, so Pitwall answers 503 and Gearbox falls back, then fails cleanly);
- Prometheus scrapes both Gearbox and Pitwall;
- Grafana has Pitwall's and Gearbox's dashboards.

Standard library only, so it runs on a bare CI runner.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def get(url: str, timeout: float = 10) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def post(url: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, json.dumps(body).encode(), method="POST")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_for(name: str, url: str, seconds: int = 300) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = get(url)
        if status == 200:
            print(f"PASS {name} is up")
            return
        time.sleep(5)
    sys.exit(f"FAIL {name} not up after {seconds}s ({url})")


def check(name: str, ok: bool, detail: object) -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {str(detail)[:400]}"))
    if not ok:
        sys.exit(1)


def main() -> None:
    wait_for("scout", "http://localhost:8001/health")
    wait_for("mlflow", "http://localhost:5000/health")
    wait_for("pitwall (metrics)", "http://localhost:8003/metrics/")
    wait_for("gearbox", "http://localhost:8002/health", seconds=600)  # loads its models

    status, body = post(
        "http://localhost:8002/v1/chat/completions",
        {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
    )
    check("gearbox fails cleanly without keys or Ollama (503)", status == 503, body)

    _, gearbox_metrics = get("http://localhost:8002/metrics/")
    check(
        "gearbox routed it with Pitwall's router (v4-finetuned)",
        'router="v4-finetuned"' in gearbox_metrics and 'status="error"' in gearbox_metrics,
        gearbox_metrics[:400],
    )
    _, pitwall_metrics = get("http://localhost:8003/metrics/")
    check(
        "pitwall received gearbox's routing request",
        'pitwall_route_errors_total{reason="model_unavailable"}' in pitwall_metrics,
        pitwall_metrics[:400],
    )

    deadline = time.time() + 120
    health: dict[str, str] = {}
    while time.time() < deadline:
        _, targets = get("http://localhost:9090/api/v1/targets")
        try:
            active = json.loads(targets)["data"]["activeTargets"]
            health = {t["labels"]["job"]: t["health"] for t in active}
        except (ValueError, KeyError):
            health = {}
        if health.get("gearbox") == "up" and health.get("pitwall") == "up":
            break
        time.sleep(5)
    check(
        "prometheus scrapes gearbox and pitwall",
        health == {"gearbox": "up", "pitwall": "up"},
        health,
    )

    for uid in ("pitwall", "gearbox"):
        status, board = get(f"http://localhost:3000/api/dashboards/uid/{uid}")
        check(f"grafana has the {uid} dashboard", status == 200, board)
    print("system smoke test passed")


if __name__ == "__main__":
    main()
