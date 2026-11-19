from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "i5_report.py"
spec = importlib.util.spec_from_file_location("i5_report", SCRIPT)
i5 = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["i5_report"] = i5  # dataclasses look their module up here
spec.loader.exec_module(i5)  # type: ignore[union-attr]


def dyno_db(path: Path, cases: dict[str, dict]) -> Path:
    """A minimal Dyno store: one finished run; cases = id -> {tags, cost, scores}."""
    db = path / "dyno.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table runs (id text, git_commit text, status text, started_at text);
        create table cases (run_id text, case_id text, tags text);
        create table results (id integer primary key, run_id text, case_id text,
                              cost_gbp real, latency_ms real, n_steps integer);
        create table scores (result_id integer, scorer text, passed boolean);
        """
    )
    con.execute("insert into runs values ('old', 'x', 'failed', '2026-01-01')")
    con.execute("insert into runs values ('r1', 'abc', 'finished', '2026-12-01')")
    for case_id, c in cases.items():
        con.execute("insert into cases values ('r1', ?, ?)", (case_id, json.dumps(c["tags"])))
        rid = con.execute(
            "insert into results (run_id, case_id, cost_gbp, latency_ms, n_steps) "
            "values ('r1', ?, ?, 30000, 10)",
            (case_id, c["cost"]),
        ).lastrowid
        for scorer, passed in c["scores"].items():
            con.execute("insert into scores values (?, ?, ?)", (rid, scorer, passed))
    con.commit()
    con.close()
    return db


def case(tags, cost, task, hall=True, quality=True):
    return {
        "tags": tags,
        "cost": cost,
        "scores": {"task_rubric": task, "hallucination": hall, "quality": quality},
    }


@pytest.fixture
def runs(tmp_path: Path):
    def make(name: str, cases: dict) -> object:
        folder = tmp_path / name
        folder.mkdir()
        return i5.load(dyno_db(folder, cases))

    return make


def test_mcnemar_exact() -> None:
    assert i5.mcnemar_p(0, 0) == 1.0
    assert i5.mcnemar_p(0, 6) == pytest.approx(2 / 64)
    assert i5.mcnemar_p(3, 3) == 1.0


def test_cheaper_and_as_good_routes_through_gearbox(runs) -> None:
    direct = runs("d", {"a": case(["web"], 0.03, True), "t": case(["unanswerable"], 0.02, True)})
    via = runs("v", {"a": case(["web"], 0.01, True), "t": case(["unanswerable"], 0.01, True)})
    shared, rows = i5.compare(direct, via)
    assert shared == ["a", "t"] and rows[0].scorer == "task_rubric"
    verdict = i5.decide(direct, via, shared, rows)
    assert verdict.passed
    assert "route Scout through Gearbox by default" in i5.render(direct, via, None)


def test_a_failed_trick_question_or_a_big_drop_keeps_scout_direct(runs) -> None:
    good = {f"w{i}": case(["web"], 0.03, True) for i in range(10)}
    direct = runs("d", {**good, "t": case(["unanswerable"], 0.02, True)})
    worse = {f"w{i}": case(["web"], 0.01, i >= 1) for i in range(10)}  # one web case fails
    via = runs("v", {**worse, "t": case(["unanswerable"], 0.01, False)})
    shared, rows = i5.compare(direct, via)
    checks = {name: ok for name, ok, _ in i5.decide(direct, via, shared, rows).checks}
    assert checks["every unanswerable question still passes"] is False
    assert checks["task success drops by at most 5 points"] is False  # 100% -> 82%
    assert checks["cost per question lower"] is True
    text = i5.render(direct, via, None)
    assert "keep Scout direct by default" in text and "worse through Gearbox: `t`" in text


def test_only_shared_cases_are_compared_and_the_latest_finished_run_is_used(runs) -> None:
    direct = runs("d", {"a": case(["web"], 0.03, True), "only-direct": case(["web"], 1.0, False)})
    via = runs("v", {"a": case(["web"], 0.01, True)})
    assert direct.run_id == "r1"
    shared, _ = i5.compare(direct, via)
    assert shared == ["a"]


def test_gearbox_counts_from_metrics() -> None:
    metrics = "\n".join(
        [
            "# HELP gearbox_requests_total x",
            'gearbox_requests_total{cache_hit="false",escalated="false",fallback="false",'
            'router="v3-embed",status="ok",tier="M"} 40.0',
            'gearbox_requests_total{cache_hit="true",escalated="false",fallback="false",'
            'router="v3-embed",status="ok",tier="M"} 2.0',
            'gearbox_requests_total{cache_hit="false",escalated="false",fallback="false",'
            'router="v3-embed",status="ok",tier="L"} 8.0',
            'gearbox_cost_gbp_total{model="anthropic/claude-haiku-5-5",tier="M"} 0.0123',
        ]
    )
    counts = i5.gearbox_counts(metrics)
    assert counts["tier"] == {"M": 42.0, "L": 8.0}
    assert counts["cache_hit"] == {"false": 48.0, "true": 2.0}
    assert counts["model_cost"] == {"anthropic/claude-haiku-5-5": 0.0123}
