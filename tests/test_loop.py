from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from stack import loop


def pitwall_dir(tmp_path: Path) -> Path:
    derived = tmp_path / "data" / "derived"
    derived.mkdir(parents=True)
    rows = [
        {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": f"q{i}"}]}
        for i in range(3)
    ]
    (derived / "test.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    return tmp_path


def fake_gearbox(router: str = "v4-finetuned", status: int = 200) -> httpx.Client:
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/alerts":
            alerts = [{"labels": {"alertname": "PitwallDrift"}, "state": "firing"}]
            return httpx.Response(200, json={"data": {"alerts": alerts}})
        seen.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        headers = {"x-gearbox-router": router, "x-gearbox-tier": "S"}
        return httpx.Response(status, json={}, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://gearbox")
    client.seen = seen  # type: ignore[attr-defined]
    return client


class FakePitwall:
    def __init__(self, drift_code: int = 0, gate: tuple[int, str] = (0, "WOULD PROMOTE")):
        self.calls: list[list[str]] = []
        self.drift_code = drift_code
        self.gate = gate

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if args[0] == "drift":
            return self.drift_code, "drift report"
        if args[:2] == ["canary", "replay"]:
            return 0, "40 requests, GBP 0.0080 -> runs/canary-20261201.jsonl"
        if args[:2] == ["canary", "report"]:
            return 0, "canary report"
        return self.gate


def test_prompts_are_the_user_turns_in_a_seeded_order(tmp_path: Path) -> None:
    folder = pitwall_dir(tmp_path)
    a = loop.prompts(folder, "test", 5, seed=1)
    assert len(a) == 5 and set(a) == {"q0", "q1", "q2"}  # repeats when n > prompts
    assert a == loop.prompts(folder, "test", 5, seed=1)
    with pytest.raises(ValueError):
        loop.prompts(folder, "nope", 5, seed=1)


def test_default_loop_is_free_and_moves_nothing(tmp_path: Path) -> None:
    client, pitwall = fake_gearbox(), FakePitwall()
    out = loop.run_loop(texts=["a"] * 30, client=client, pitwall=pitwall, echo=lambda s: None)
    assert out.traffic.ok == 30 and out.drift is False and out.gate == "would promote"
    assert all(r["headers"]["x-gearbox-max-tier"] == "S" for r in client.seen)
    assert pitwall.calls == [
        ["drift", "check", "--window", "30"],
        ["promote", "candidate", "--dry-run"],
    ]
    assert out.alerts == ["PitwallDrift"]


def test_drift_is_reported_not_a_failure_and_a_rejection_is_a_result() -> None:
    pitwall = FakePitwall(drift_code=3, gate=(1, "FAIL under_routing\nREJECTED: v3 vs v2"))
    out = loop.run_loop(
        texts=["a"] * 30, client=fake_gearbox(), pitwall=pitwall, echo=lambda s: None
    )
    assert out.drift is True and out.gate == "rejected"


def test_promote_runs_the_canary_then_the_real_gate() -> None:
    pitwall = FakePitwall(gate=(0, "PROMOTED: v3 vs v2"))
    out = loop.run_loop(
        texts=["a"], client=fake_gearbox(), pitwall=pitwall, echo=lambda s: None, promote=True
    )
    assert [c[:2] for c in pitwall.calls] == [
        ["canary", "replay"],
        ["canary", "report"],
        ["promote", "candidate"],
    ]
    assert "--dry-run" not in pitwall.calls[-1] and out.gate == "promoted"
    assert out.canary_rows == "runs/canary-20261201.jsonl"


@pytest.mark.parametrize(
    ("client", "pitwall", "message"),
    [
        (fake_gearbox(status=503), FakePitwall(), "answered"),
        (fake_gearbox(router="v3-embed"), FakePitwall(), "didn't route through Pitwall"),
        (fake_gearbox(), FakePitwall(gate=(1, "error: MLflow down")), "gate couldn't run"),
    ],
)
def test_stops_on_an_unexpected_result(client, pitwall, message) -> None:
    with pytest.raises(loop.LoopStopped, match=message):
        loop.run_loop(texts=["a"], client=client, pitwall=pitwall, echo=lambda s: None)


def test_a_candidate_already_in_production_is_nothing_to_release() -> None:
    pitwall = FakePitwall(gate=(1, "error: v2 is already production"))
    out = loop.run_loop(texts=["a"], client=fake_gearbox(), pitwall=pitwall, echo=lambda s: None)
    assert out.gate == "nothing to release"


def test_the_gate_can_check_a_named_version() -> None:
    pitwall = FakePitwall(gate=(1, "REJECTED: v1 vs v2"))
    out = loop.run_loop(
        texts=["a"], client=fake_gearbox(), pitwall=pitwall, echo=lambda s: None, gate_ref="v1"
    )
    assert pitwall.calls[-1] == ["promote", "v1", "--dry-run"] and out.gate == "rejected"


def test_noise_is_dropped_from_pitwalls_output() -> None:
    shown: list[str] = []
    loop._show(shown.append, "INFO mlflow.agent.hint: x\nLoading weights: 50%\n\nkept line")
    assert shown == ["  | kept line"]


def test_drift_check_failure_stops_the_loop() -> None:
    with pytest.raises(loop.LoopStopped, match="drift check failed"):
        loop.run_loop(
            texts=["a"] * 30,
            client=fake_gearbox(),
            pitwall=FakePitwall(drift_code=1),
            echo=lambda s: None,
        )


def mixed_gearbox(routers: list[str]) -> httpx.Client:
    """Answers with these routers in turn ('cache' = Gearbox's cache, not Pitwall)."""
    order = iter(routers)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/alerts":
            return httpx.Response(200, json={"data": {"alerts": []}})
        headers = {"x-gearbox-router": next(order), "x-gearbox-tier": "S"}
        return httpx.Response(200, json={}, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://gearbox")


def test_cached_answers_dont_count_toward_the_drift_window() -> None:
    pitwall = FakePitwall()
    client = mixed_gearbox(["v4-finetuned"] * 32 + ["cache"] * 5 + ["v4-candidate"] * 3)
    out = loop.run_loop(texts=["a"] * 40, client=client, pitwall=pitwall, echo=lambda s: None)
    assert pitwall.calls[0] == ["drift", "check", "--window", "32"] and out.drift is False


def test_too_few_routed_prompts_skip_the_drift_check() -> None:
    pitwall = FakePitwall()
    client = mixed_gearbox(["v4-finetuned"] * 20 + ["cache"] * 20)
    out = loop.run_loop(texts=["a"] * 40, client=client, pitwall=pitwall, echo=lambda s: None)
    assert out.drift is None and pitwall.calls[0][0] == "promote"
