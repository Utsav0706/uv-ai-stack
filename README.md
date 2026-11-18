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
the pinned versions that were tested together, and (in later steps) one command to run
the whole system and the loop that goes through all four.

**Status:** I1 of the integration, pinned versions.

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

## Integration steps

| Step | What | Paid cost |
|---|---|---|
| I1 | This repository, pinned versions, CI | £0 |
| I2 | Gearbox metrics (incl. cost) in Pitwall's Prometheus and Grafana | £0 |
| I3 | Gearbox's live evaluation through Dyno | ~£0.10 |
| I4 | One command to start the whole system; Compose file checked in CI | £0 |
| I5 | Scout through Gearbox, judged by Dyno on the full golden set; the loop script | ~£2.50 |
| I6 | This README: architecture, the loop, headline numbers | £0 |
