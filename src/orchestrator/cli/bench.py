"""Bench orchestration for `democtl bench`.

Creates `n` tagged environments (respecting `--parallel`), waits for each to
reach Ready or Failed, then waits for every created environment to reach a
terminal ledger event (`deleted`, `delete_timeout` or `sweeper_reaped`) before
computing metrics filtered to just this run.

Built around injectable `sleep`/`clock`/`ledger_events`/`make_env_name`
callables so the whole flow is unit-testable without a real cluster or real
time (see `tests/unit/test_bench.py`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from orchestrator.core.ledger import EventType, LedgerEvent
from orchestrator.core.metrics import MetricsReport, compute_metrics
from orchestrator.core.personas import Persona
from orchestrator.core.pricing import Pricing

# How often to poll a freshly-created env for Ready/Failed.
DEFAULT_CREATE_POLL_SECONDS = 2.0
# How often to poll the ledger for terminal events, per the task brief ("~15s").
DEFAULT_LEDGER_POLL_SECONDS = 15.0

_TERMINAL_EVENTS = frozenset(
    {EventType.DELETED, EventType.DELETE_TIMEOUT, EventType.SWEEPER_REAPED}
)


class BenchKube(Protocol):
    """The slice of `CliKube` bench needs to create and poll environments."""

    def create_env(self, name: str, spec: dict[str, Any]) -> None: ...

    def get_env(self, name: str) -> dict[str, Any] | None: ...


@dataclass
class BenchEnvResult:
    """One bench environment's outcome, for the summary line and progress output."""

    name: str
    created: bool = False
    ready: bool = False
    failed: bool = False
    ready_seconds: float | None = None
    terminal_seen: bool = False


@dataclass
class BenchResult:
    tag: str
    envs: list[BenchEnvResult] = field(default_factory=list)
    metrics: MetricsReport | None = None


def generate_tag(now: datetime) -> str:
    """The `requestedBy` tag for a bench run: `bench-<UTC compact timestamp>`."""
    return f"bench-{now.strftime('%Y%m%dT%H%M%SZ')}"


def _create_next(
    kube: BenchKube,
    name: str,
    *,
    persona: str,
    ttl: str,
    tag: str,
    result: BenchEnvResult,
    on_progress: Callable[[str], None],
) -> bool:
    """Create one env; returns True if it's now in flight, False if creation itself failed."""
    try:
        kube.create_env(name, {"persona": persona, "ttl": ttl, "requestedBy": tag})
    except Exception as exc:
        result.failed = True
        on_progress(f"{name}: create failed: {exc}")
        return False
    result.created = True
    on_progress(f"{name}: created")
    return True


def _poll_creation(
    kube: BenchKube,
    in_flight: dict[str, float],
    results: dict[str, BenchEnvResult],
    *,
    timeout_per_env: float,
    clock: Callable[[], float],
    on_progress: Callable[[str], None],
) -> None:
    """One pass over envs waiting for Ready/Failed; mutates `in_flight` and `results`."""
    for name in list(in_flight):
        try:
            env = kube.get_env(name)
        except Exception as exc:
            on_progress(f"{name}: get_env error, will retry: {exc}")
            continue
        phase = ((env or {}).get("status") or {}).get("phase")
        started_at = in_flight[name]
        if phase == "Ready":
            elapsed = clock() - started_at
            in_flight.pop(name)
            results[name].ready = True
            results[name].ready_seconds = elapsed
            on_progress(f"{name}: Ready ({elapsed:.1f}s)")
        elif phase == "Failed":
            in_flight.pop(name)
            results[name].failed = True
            on_progress(f"{name}: Failed")
        elif clock() - started_at > timeout_per_env:
            in_flight.pop(name)
            results[name].failed = True
            on_progress(f"{name}: timed out waiting for Ready after {timeout_per_env:.0f}s")


def _create_and_wait_ready(
    kube: BenchKube,
    names: list[str],
    results: dict[str, BenchEnvResult],
    *,
    persona: str,
    ttl: str,
    tag: str,
    parallel: int,
    timeout_per_env: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    on_progress: Callable[[str], None],
    poll_interval: float,
) -> None:
    in_flight: dict[str, float] = {}
    next_idx = 0

    def start_up_to_capacity() -> None:
        nonlocal next_idx
        while next_idx < len(names) and len(in_flight) < parallel:
            name = names[next_idx]
            next_idx += 1
            if _create_next(
                kube,
                name,
                persona=persona,
                ttl=ttl,
                tag=tag,
                result=results[name],
                on_progress=on_progress,
            ):
                in_flight[name] = clock()

    start_up_to_capacity()
    while in_flight:
        _poll_creation(
            kube,
            in_flight,
            results,
            timeout_per_env=timeout_per_env,
            clock=clock,
            on_progress=on_progress,
        )
        start_up_to_capacity()
        if in_flight:
            sleep(poll_interval)


def _wait_for_terminal_events(
    created_names: list[str],
    results: dict[str, BenchEnvResult],
    *,
    ledger_events: Callable[[], Iterable[LedgerEvent]],
    deadline_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    on_progress: Callable[[str], None],
    poll_interval: float,
) -> list[LedgerEvent]:
    """Poll the ledger until every created env has a terminal event, or the deadline passes.

    Returns the last-read event list (used to compute the final metrics).
    """
    pending = set(created_names)
    events: list[LedgerEvent] = []
    deadline = clock() + deadline_seconds
    while True:
        events = list(ledger_events())
        terminal_envs = {e.env for e in events if e.event in _TERMINAL_EVENTS}
        for name in list(pending):
            if name in terminal_envs:
                pending.discard(name)
                results[name].terminal_seen = True
                on_progress(f"{name}: terminal event seen")
        if not pending:
            return events
        if clock() >= deadline:
            on_progress(f"timed out waiting for terminal events: {sorted(pending)}")
            return events
        sleep(poll_interval)


def run_bench(
    kube: BenchKube,
    *,
    persona: str,
    n: int,
    ttl: str,
    tag: str,
    make_env_name: Callable[[int], str],
    ledger_events: Callable[[], Iterable[LedgerEvent]],
    parallel: int = 1,
    timeout_per_env: float = 600.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    pricing: Pricing | None = None,
    personas: dict[str, Persona] | None = None,
    on_progress: Callable[[str], None] = lambda _msg: None,
    create_poll_interval: float = DEFAULT_CREATE_POLL_SECONDS,
    ledger_poll_interval: float = DEFAULT_LEDGER_POLL_SECONDS,
) -> BenchResult:
    """Run one bench cycle: create `n` tagged envs, wait for Ready, wait for teardown, report."""
    names = [make_env_name(i) for i in range(n)]
    results: dict[str, BenchEnvResult] = {name: BenchEnvResult(name=name) for name in names}

    _create_and_wait_ready(
        kube,
        names,
        results,
        persona=persona,
        ttl=ttl,
        tag=tag,
        parallel=parallel,
        timeout_per_env=timeout_per_env,
        sleep=sleep,
        clock=clock,
        on_progress=on_progress,
        poll_interval=create_poll_interval,
    )

    created_names = [name for name in names if results[name].created]
    events = _wait_for_terminal_events(
        created_names,
        results,
        ledger_events=ledger_events,
        deadline_seconds=n * timeout_per_env,
        sleep=sleep,
        clock=clock,
        on_progress=on_progress,
        poll_interval=ledger_poll_interval,
    )

    bench_env_names = {
        e.env
        for e in events
        if e.event is EventType.REQUESTED and e.details.get("requestedBy") == tag
    }
    filtered_events = [e for e in events if e.env in bench_env_names]
    report = compute_metrics(filtered_events, pricing=pricing, personas=personas)

    return BenchResult(tag=tag, envs=list(results.values()), metrics=report)
