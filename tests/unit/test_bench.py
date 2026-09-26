"""Unit tests for `cli.bench.run_bench`, with a fake kube, fake ledger and fake clock/sleep.

No cluster, no real time: `sleep` is a no-op that just advances a fake clock,
so `--parallel` and terminal-event waits can be exercised deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from orchestrator.cli.bench import BenchKube, generate_tag, run_bench
from orchestrator.core.expiry import to_rfc3339
from orchestrator.core.ledger import EventType, LedgerEvent


class FakeClock:
    """A fake monotonic clock; `sleep` advances it instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBenchKube(BenchKube):
    """In-memory `BenchKube`: envs go Ready after a fixed number of polls."""

    def __init__(self, *, ready_after_polls: int = 1, fail_names: set[str] | None = None) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self._poll_count: dict[str, int] = {}
        self._ready_after_polls = ready_after_polls
        self._fail_names = fail_names or set()

    def create_env(self, name: str, spec: dict[str, Any]) -> None:
        self.created.append((name, dict(spec)))
        self._poll_count[name] = 0

    def get_env(self, name: str) -> dict[str, Any] | None:
        self._poll_count[name] += 1
        if name in self._fail_names:
            return {"status": {"phase": "Failed"}}
        if self._poll_count[name] >= self._ready_after_polls:
            return {"status": {"phase": "Ready"}}
        return {"status": {"phase": "Pending"}}


def _ev(env: str, event: EventType, ts_seconds: float, **details: object) -> LedgerEvent:
    ts = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC).replace(microsecond=0)
    from datetime import timedelta

    return LedgerEvent(
        ts=ts + timedelta(seconds=ts_seconds),
        event=event,
        env=env,
        namespace=f"demo-{env}",
        persona="healthcare",
        actor="operator",
        details=details,
    )


def test_generate_tag_format() -> None:
    tag = generate_tag(datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC))
    assert tag == "bench-20260924T120000Z"


def test_run_bench_creates_n_envs_tagged() -> None:
    kube = FakeBenchKube(ready_after_polls=1)
    clock = FakeClock()
    tag = "bench-test"
    names = [f"healthcare-{i:04d}" for i in range(3)]

    ledger_events_calls = {"n": 0}

    def ledger_events() -> list[LedgerEvent]:
        ledger_events_calls["n"] += 1
        return [_ev(name, EventType.REQUESTED, 0, requestedBy=tag) for name in names] + [
            _ev(name, EventType.DELETED, 10, lag_seconds=1) for name in names
        ]

    result = run_bench(
        kube,
        persona="healthcare",
        n=3,
        ttl="2m",
        tag=tag,
        make_env_name=lambda i: names[i],
        ledger_events=ledger_events,
        parallel=1,
        timeout_per_env=60.0,
        sleep=clock.sleep,
        clock=clock.clock,
    )

    assert len(kube.created) == 3
    for _name, spec in kube.created:
        assert spec["requestedBy"] == tag
        assert spec["persona"] == "healthcare"
        assert spec["ttl"] == "2m"
    assert {e.name for e in result.envs} == set(names)
    assert all(e.ready for e in result.envs)
    assert all(e.terminal_seen for e in result.envs)
    assert result.metrics is not None
    assert result.metrics.provisioning.n == 0  # no `ready` events in this fixture


def test_run_bench_respects_parallel_limit() -> None:
    # Never go Ready on its own; we just want to observe how many are created
    # before the first poll happens, to check the in-flight cap.
    kube = FakeBenchKube(ready_after_polls=1000)
    clock = FakeClock()
    names = [f"healthcare-{i:04d}" for i in range(5)]

    create_times: list[float] = []
    real_create = kube.create_env

    def logging_create(name: str, spec: dict[str, Any]) -> None:
        real_create(name, spec)
        create_times.append(clock.clock())

    kube.create_env = logging_create  # type: ignore[method-assign]

    def ledger_events() -> list[LedgerEvent]:
        return []

    # timeout_per_env small so the run terminates quickly once the cap is observed.
    run_bench(
        kube,
        persona="healthcare",
        n=5,
        ttl="2m",
        tag="bench-test",
        make_env_name=lambda i: names[i],
        ledger_events=ledger_events,
        parallel=2,
        timeout_per_env=1.0,
        sleep=clock.sleep,
        clock=clock.clock,
    )

    # All 5 are eventually created (they keep timing out, freeing capacity), but
    # only 2 are created before any poll/sleep happens: the --parallel cap.
    assert len(create_times) == 5
    assert create_times[:2] == [0.0, 0.0]
    assert create_times[2] > 0.0


def test_run_bench_handles_failed_env_and_counts_it() -> None:
    kube = FakeBenchKube(ready_after_polls=1, fail_names={"healthcare-0001"})
    clock = FakeClock()
    tag = "bench-test"
    names = ["healthcare-0000", "healthcare-0001"]

    def ledger_events() -> list[LedgerEvent]:
        return [_ev(name, EventType.REQUESTED, 0, requestedBy=tag) for name in names] + [
            _ev(name, EventType.DELETED, 10, lag_seconds=1) for name in names
        ]

    result = run_bench(
        kube,
        persona="healthcare",
        n=2,
        ttl="2m",
        tag=tag,
        make_env_name=lambda i: names[i],
        ledger_events=ledger_events,
        parallel=2,
        timeout_per_env=60.0,
        sleep=clock.sleep,
        clock=clock.clock,
    )

    by_name = {e.name: e for e in result.envs}
    assert by_name["healthcare-0000"].ready
    assert by_name["healthcare-0001"].failed
    assert not by_name["healthcare-0001"].ready


def test_run_bench_filters_metrics_to_tag_excluding_unrelated_env() -> None:
    kube = FakeBenchKube(ready_after_polls=1)
    clock = FakeClock()
    tag = "bench-test"
    names = ["healthcare-0000"]

    def ledger_events() -> list[LedgerEvent]:
        return [
            _ev(names[0], EventType.REQUESTED, 0, requestedBy=tag),
            _ev(names[0], EventType.READY, 30, provisioning_seconds=30),
            _ev(names[0], EventType.DELETED, 40, lag_seconds=1),
            # An unrelated env, requested by a human, not this bench tag.
            _ev("healthcare-unrelated", EventType.REQUESTED, 0, requestedBy="evan"),
            _ev("healthcare-unrelated", EventType.READY, 500, provisioning_seconds=500),
            _ev("healthcare-unrelated", EventType.DELETED, 600, lag_seconds=1),
        ]

    result = run_bench(
        kube,
        persona="healthcare",
        n=1,
        ttl="2m",
        tag=tag,
        make_env_name=lambda i: names[i],
        ledger_events=ledger_events,
        parallel=1,
        timeout_per_env=60.0,
        sleep=clock.sleep,
        clock=clock.clock,
    )

    assert result.metrics is not None
    assert result.metrics.provisioning.n == 1
    assert result.metrics.provisioning.p50 == 30.0  # not 500 (the unrelated env)


def test_run_bench_waits_for_terminal_events_with_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    kube = FakeBenchKube(ready_after_polls=1)
    clock = FakeClock()
    tag = "bench-test"
    names = ["healthcare-0000"]

    poll_state = {"n": 0}

    def ledger_events() -> list[LedgerEvent]:
        poll_state["n"] += 1
        if poll_state["n"] < 3:
            return [_ev(names[0], EventType.REQUESTED, 0, requestedBy=tag)]
        return [
            _ev(names[0], EventType.REQUESTED, 0, requestedBy=tag),
            _ev(names[0], EventType.DELETED, 10, lag_seconds=1),
        ]

    messages: list[str] = []
    result = run_bench(
        kube,
        persona="healthcare",
        n=1,
        ttl="2m",
        tag=tag,
        make_env_name=lambda i: names[i],
        ledger_events=ledger_events,
        parallel=1,
        timeout_per_env=60.0,
        sleep=clock.sleep,
        clock=clock.clock,
        on_progress=messages.append,
        ledger_poll_interval=1.0,
    )

    assert poll_state["n"] >= 3
    assert result.envs[0].terminal_seen
    assert any("terminal event seen" in m for m in messages)
    assert any("created" in m for m in messages)
    assert any("Ready" in m for m in messages)


def test_to_rfc3339_used_in_fixture_smoke() -> None:
    # Sanity: the shared `_ev` timestamps are well-formed RFC3339 once serialized.
    e = _ev("x", EventType.REQUESTED, 0)
    assert to_rfc3339(e.ts).endswith("Z")
