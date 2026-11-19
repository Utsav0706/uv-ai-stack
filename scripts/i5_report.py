"""Integration step I5: Scout direct vs Scout through Gearbox, judged by Dyno.

    python scripts/i5_report.py DIRECT_DIR VIA_GEARBOX_DIR [--out reports/i5-scout-via-gearbox.md]

Each folder is one downloaded artifact of Dyno's eval workflow (dyno.db, plus
out/gearbox-metrics.txt for the run through Gearbox). Only the cases both runs share are
compared, case by case. Reads Dyno's SQLite store directly with the standard library:
this repository never imports a project's code.

The decision rule was fixed before the results (README, step I5): Scout's default
becomes "through Gearbox" only if, on the full golden set,
- task success drops by at most 5 points,
- every unanswerable (trick) question still passes,
- the hallucination pass rate is no lower,
- the cost per question is lower.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

SCORERS = ("task_rubric", "hallucination", "quality")
MAX_TASK_DROP = 0.05


@dataclass
class Case:
    tags: list[str]
    passed: dict[str, float] = field(default_factory=dict)  # scorer -> share of attempts
    cost_gbp: float = 0.0  # mean over attempts
    latency_s: float = 0.0
    steps: float = 0.0


@dataclass
class Run:
    run_id: str
    commit: str | None
    cases: dict[str, Case]


def load(db: Path) -> Run:
    """The latest finished run in a Dyno store, per case."""
    import json

    con = sqlite3.connect(db)
    row = con.execute(
        "select id, git_commit from runs where status = 'finished' order by started_at desc"
    ).fetchone()
    if row is None:
        raise SystemExit(f"no finished run in {db}")
    run_id, commit = row
    cases: dict[str, Case] = {}
    for case_id, tags in con.execute("select case_id, tags from cases where run_id = ?", (run_id,)):
        cases[case_id] = Case(tags=json.loads(tags))
    results = con.execute(
        "select id, case_id, cost_gbp, latency_ms, n_steps from results where run_id = ?",
        (run_id,),
    ).fetchall()
    per_case: dict[str, list[tuple[int, float, float, int]]] = {}
    for rid, case_id, cost, latency, steps in results:
        per_case.setdefault(case_id, []).append((rid, cost or 0.0, latency, steps))
    for case_id, attempts in per_case.items():
        case = cases.setdefault(case_id, Case(tags=[]))
        case.cost_gbp = statistics.mean(a[1] for a in attempts)
        case.latency_s = statistics.mean(a[2] for a in attempts) / 1000
        case.steps = statistics.mean(a[3] for a in attempts)
        for scorer in SCORERS:
            marks = []
            for rid, *_ in attempts:
                row = con.execute(
                    "select passed from scores where result_id = ? and scorer = ?", (rid, scorer)
                ).fetchone()
                if row and row[0] is not None:  # None: the scorer skipped or failed
                    marks.append(bool(row[0]))
            if marks:
                case.passed[scorer] = sum(marks) / len(marks)
    con.close()
    return Run(run_id, commit, cases)


def mcnemar_p(b: int, c: int) -> float:
    """Exact two-sided McNemar test: b, c = cases that changed in each direction."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


@dataclass
class ScorerRow:
    scorer: str
    n: int
    direct: float
    via: float
    worse: list[str]  # passed direct, failed through Gearbox
    better: list[str]
    p: float


def compare(direct: Run, via: Run) -> tuple[list[str], list[ScorerRow]]:
    shared = sorted(set(direct.cases) & set(via.cases))
    rows = []
    for scorer in SCORERS:
        ids = [
            c for c in shared if scorer in direct.cases[c].passed and scorer in via.cases[c].passed
        ]
        if not ids:
            continue
        d = [direct.cases[c].passed[scorer] >= 0.5 for c in ids]
        v = [via.cases[c].passed[scorer] >= 0.5 for c in ids]
        worse = [c for c, a, b in zip(ids, d, v, strict=True) if a and not b]
        better = [c for c, a, b in zip(ids, d, v, strict=True) if b and not a]
        rows.append(
            ScorerRow(
                scorer,
                len(ids),
                sum(d) / len(ids),
                sum(v) / len(ids),
                worse,
                better,
                mcnemar_p(len(worse), len(better)),
            )
        )
    return shared, rows


def gearbox_counts(metrics: str) -> dict[str, dict[str, float]]:
    """Requests by tier, cost by model and cache hits from Gearbox's /metrics text."""
    out: dict[str, dict[str, float]] = {"tier": {}, "model_cost": {}, "cache_hit": {}}
    for line in metrics.splitlines():
        m = re.match(r"(gearbox_requests_total|gearbox_cost_gbp_total)\{(.*)\} (\S+)$", line)
        if not m:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        value = float(m.group(3))
        if m.group(1) == "gearbox_requests_total":
            tier = labels.get("tier", "?")
            out["tier"][tier] = out["tier"].get(tier, 0) + value
            hit = labels.get("cache_hit", "?")
            out["cache_hit"][hit] = out["cache_hit"].get(hit, 0) + value
        else:
            model = labels.get("model", "?")
            out["model_cost"][model] = out["model_cost"].get(model, 0) + value
    return out


@dataclass
class Verdict:
    checks: list[tuple[str, bool, str]]

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def decide(direct: Run, via: Run, shared: list[str], rows: list[ScorerRow]) -> Verdict:
    by = {r.scorer: r for r in rows}
    checks = []
    task = by.get("task_rubric")
    if task:
        drop = task.direct - task.via
        checks.append(
            (
                f"task success drops by at most {MAX_TASK_DROP * 100:.0f} points",
                drop <= MAX_TASK_DROP + 1e-9,
                f"{task.direct:.0%} -> {task.via:.0%}",
            )
        )
    trick = [c for c in shared if "unanswerable" in via.cases[c].tags]
    trick_ok = [c for c in trick if via.cases[c].passed.get("task_rubric", 0) >= 0.5]
    checks.append(
        (
            "every unanswerable question still passes",
            bool(trick) and len(trick_ok) == len(trick),
            f"{len(trick_ok)}/{len(trick)}",
        )
    )
    hall = by.get("hallucination")
    if hall:
        checks.append(
            (
                "hallucination pass rate no lower",
                hall.via >= hall.direct - 1e-9,
                f"{hall.direct:.0%} -> {hall.via:.0%}",
            )
        )
    cost_d = statistics.mean(direct.cases[c].cost_gbp for c in shared)
    cost_v = statistics.mean(via.cases[c].cost_gbp for c in shared)
    checks.append(
        ("cost per question lower", cost_v < cost_d, f"GBP {cost_d:.4f} -> GBP {cost_v:.4f}")
    )
    return Verdict(checks)


def render(direct: Run, via: Run, metrics: str | None) -> str:
    shared, rows = compare(direct, via)
    verdict = decide(direct, via, shared, rows)
    mean = statistics.mean
    decision = (
        "route Scout through Gearbox by default"
        if verdict.passed
        else "keep Scout direct by default"
    )
    median_d = statistics.median(direct.cases[c].latency_s for c in shared)
    median_v = statistics.median(via.cases[c].latency_s for c in shared)
    lines = [
        "# I5: Scout direct vs through Gearbox, judged by Dyno",
        "",
        f"Generated by `scripts/i5_report.py` from Dyno runs `{direct.run_id}` (direct) and "
        f"`{via.run_id}` (through Gearbox): {len(shared)} shared cases.",
        "",
        f"**Decision: {decision}.**",
        "",
        "| Rule (fixed before the run) | Result | |",
        "|---|---|---|",
    ]
    lines += [
        f"| {name} | {detail} | {'pass' if ok else 'FAIL'} |" for name, ok, detail in verdict.checks
    ]
    lines += [
        "",
        "## Judged by Dyno",
        "",
        "| Scorer | Cases | Direct | Through Gearbox | Worse | Better | McNemar p |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r.scorer} | {r.n} | {r.direct:.0%} | {r.via:.0%} | {len(r.worse)} "
        f"| {len(r.better)} | {r.p:.2f} |"
        for r in rows
    ]
    lines += [
        "",
        "*Worse*: passed direct, failed through Gearbox (and *Better* the reverse). The exact",
        "McNemar test uses only those changed cases; with one attempt per case, a p above",
        "0.05 means the difference can't be told apart from run-to-run noise.",
        "",
        "## Cost and time",
        "",
        "| | Direct | Through Gearbox |",
        "|---|---|---|",
        f"| Cost per question (mean) | GBP {mean(direct.cases[c].cost_gbp for c in shared):.4f} "
        f"| GBP {mean(via.cases[c].cost_gbp for c in shared):.4f} |",
        f"| Time per question (median) | {median_d:.0f} s | {median_v:.0f} s |",
        f"| Steps per question (mean) | {mean(direct.cases[c].steps for c in shared):.1f} "
        f"| {mean(via.cases[c].steps for c in shared):.1f} |",
    ]
    tags = sorted({t for c in shared for t in direct.cases[c].tags} - {"ci"})
    if tags:
        lines += [
            "",
            "## Task success by kind of question",
            "",
            "| Tag | Cases | Direct | Through Gearbox |",
            "|---|---|---|---|",
        ]
        for tag in tags:
            ids = [c for c in shared if tag in direct.cases[c].tags]
            d = sum(direct.cases[c].passed.get("task_rubric", 0) >= 0.5 for c in ids)
            v = sum(via.cases[c].passed.get("task_rubric", 0) >= 0.5 for c in ids)
            lines.append(f"| {tag} | {len(ids)} | {d}/{len(ids)} | {v}/{len(ids)} |")
    task = next((r for r in rows if r.scorer == "task_rubric"), None)
    if task and (task.worse or task.better):
        lines += ["", "## Cases that changed (task success)", ""]
        lines += [f"- worse through Gearbox: `{c}`" for c in task.worse]
        lines += [f"- better through Gearbox: `{c}`" for c in task.better]
    if metrics:
        counts = gearbox_counts(metrics)
        total = sum(counts["tier"].values())
        lines += ["", "## What Gearbox saw", "", f"{total:.0f} model calls from Scout.", ""]
        for tier, n in sorted(counts["tier"].items()):
            lines.append(f"- tier {tier}: {n:.0f} calls ({n / total:.0%})")
        for model, cost in sorted(counts["model_cost"].items()):
            lines.append(f"- {model}: GBP {cost:.4f}")
        hits = counts["cache_hit"].get("true", 0)
        lines.append(f"- cache hits: {hits:.0f}")
    lines += [
        "",
        f"Dyno commit: direct `{(direct.commit or '?')[:12]}`, through Gearbox "
        f"`{(via.commit or '?')[:12]}`; Scout's and Gearbox's are in each workflow run's inputs.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("direct", type=Path)
    parser.add_argument("via_gearbox", type=Path)
    parser.add_argument("--out", type=Path, default=Path("reports/i5-scout-via-gearbox.md"))
    args = parser.parse_args()
    direct = load(args.direct / "dyno.db")
    via = load(args.via_gearbox / "dyno.db")
    metrics_file = args.via_gearbox / "out" / "gearbox-metrics.txt"
    metrics = metrics_file.read_text(encoding="utf-8") if metrics_file.exists() else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(direct, via, metrics), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
