# uv-ai-stack

[![CI](https://github.com/Utsav0706/uv-ai-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/Utsav0706/uv-ai-stack/actions/workflows/ci.yml)

Four projects that work on their own and connect into one system:

| Project | What it does |
|---|---|
| [Scout](https://github.com/Utsav0706/scout) | Research agent: plans, searches, reads, writes and verifies a cited report |
| [Gearbox](https://github.com/Utsav0706/gearbox) | LLM router: picks the cheapest model tier that answers a request well |
| [Dyno](https://github.com/Utsav0706/dyno) | Agent eval harness: cases, scorers, calibrated judges, a CI gate |
| [Pitwall](https://github.com/Utsav0706/pitwall) | LLMOps for Gearbox's router model: data, training, registry, gate, canary, monitoring, drift, retraining |

They connect only through HTTP APIs, command lines and shared files (JSONL, YAML), never
by importing each other's code. This repository holds what belongs to the combination:
the pinned versions that were tested together, one command to run the whole system, and
(in a later step) the loop that goes through all four.

**Status:** I4 done: `stack up` runs the whole system; CI builds and checks it in Docker Compose.

## Pinned versions

[`stack.toml`](stack.toml) pins each project to a commit. `stack pins` compares the pins
with local checkouts (`../scout`, `../gearbox`, ...); `stack pins --update` moves them on
purpose; CI checks every pinned commit exists on GitHub.

```bash
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
stack pins
```

## Run the system

On Windows, from each project's own virtual environment (each checkout next to this one,
set up as its README says; Ollama running):

```bash
stack up
stack status
stack down
```

`stack up` starts MLflow, Pitwall, Gearbox, Scout, Prometheus and Grafana in dependency
order, waits for each health check, and reuses anything already running. Gearbox routes
through Pitwall (router `v4-finetuned`, 10% canary to `v4-candidate`) using a config
generated in `runs/`, so no project's repository is changed. `--ui` adds the three
Streamlit apps; `--scout-via-gearbox` sends Scout's model calls through Gearbox.
`stack status` shows health, process and memory; `stack down` stops only what `stack up`
started.

| Service | Port |
|---|---|
| Scout API | 8001 |
| Gearbox API (`/metrics` for Prometheus) | 8002 |
| Pitwall API | 8003 |
| MLflow | 5000 |
| Prometheus | 9090 |
| Grafana | 3000 |
| Scout UI, Gearbox dashboard, Dyno dashboard (`--ui`) | 8501, 8502, 8503 |

On Linux or with Docker, [`docker-compose.yml`](docker-compose.yml) runs the same system
with each project's stores (Postgres, Redis):

```bash
stack config gearbox --pitwall-url http://pitwall:8003 --out runs/gearbox-compose.yaml
docker compose up -d --build
```

CI's `system` job checks out the four pinned commits, builds and starts that file, and
runs [`scripts/system_smoke.py`](scripts/system_smoke.py): every service healthy, a
request to Gearbox routed through Pitwall (both record it), Prometheus scraping both,
Grafana's dashboards loaded. CI has no model keys, so the request ends in a clean 503.

## Integration steps

| Step | What | Paid cost |
|---|---|---|
| I1 | This repository, pinned versions, CI | £0 |
| I2 | Gearbox metrics (incl. cost) in Pitwall's Prometheus and Grafana | £0 |
| I3 | Gearbox's live evaluation through Dyno | ~£0.10 |
| I4 | One command to start the whole system; Compose file checked in CI | £0 |
| I5 | Scout through Gearbox, judged by Dyno on the full golden set; the loop script | ~£2.50 |
| I6 | This README: architecture, the loop, headline numbers | £0 |
