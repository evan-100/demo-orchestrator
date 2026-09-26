"""Ledger-derived metrics: provisioning time, cleanup reliability, cost delta.

Everything is computed by replaying the JSONL audit ledger (spec A7); nothing
here talks to the cluster. `compute_metrics` groups events by environment
name, de-duplicates retried/duplicate events (Ruling R15: first by ts wins),
then reports three independent sections. Percentiles use the nearest-rank
method: for `p` in [0, 100] and `n` sorted values, the rank is
`ceil(p/100 * n)` (clamped to at least 1), and the value at that rank (1-based)
is the percentile; an empty sample reports 0.0 everywhere without dividing by
zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil

from orchestrator.core.durations import InvalidDurationError, parse_duration
from orchestrator.core.expiry import from_rfc3339
from orchestrator.core.ledger import EventType, LedgerEvent
from orchestrator.core.personas import Persona
from orchestrator.core.pricing import Pricing

DEFAULT_GRACE = timedelta(seconds=120)

# The terminal events of an environment's lifecycle: whichever comes first (by
# ts) closes the environment out. Anything after it is a duplicate ignored
# under Ruling R15 (e.g. the sweeper reaping a namespace the operator's
# finalizer had already torn down and logged `deleted` for).
_TERMINAL_EVENTS = frozenset(
    {EventType.DELETED, EventType.DELETE_TIMEOUT, EventType.SWEEPER_REAPED}
)


@dataclass
class ProvisioningStats:
    n: int
    p50: float
    p95: float
    max: float
    mean_timings: dict[str, float]
    failed: int = 0


@dataclass
class CleanupStats:
    expired: int
    on_time: int
    reliability: float
    lag_p50: float
    lag_p95: float
    reaped_by_sweeper: int
    delete_timeouts: int
    in_flight: int = 0


@dataclass
class CostStats:
    on_demand_usd: float
    baseline_usd: float
    savings_pct: float
    window_hours: float
    pricing_source: str


@dataclass
class MetricsReport:
    provisioning: ProvisioningStats
    cleanup: CleanupStats
    cost: CostStats | None
    warnings: list[str] = field(default_factory=list)


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Empty input reports 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = min(len(ordered), max(1, ceil(pct / 100 * len(ordered))))
    return ordered[rank - 1]


@dataclass
class _EnvRecord:
    """One environment's story so far, folded from possibly-duplicated events.

    `requested`/`ready` keep the earliest occurrence (Ruling R15: first by ts
    wins for retried events). `terminal` keeps the earliest of
    {deleted, delete_timeout, sweeper_reaped} — the event that actually closed
    the environment out; anything after it (e.g. the sweeper reaping a
    namespace the operator's finalizer already tore down) is ignored.
    """

    persona: str | None = None
    requested: LedgerEvent | None = None
    ready: LedgerEvent | None = None
    failed: LedgerEvent | None = None
    expired: LedgerEvent | None = None
    last_extended: LedgerEvent | None = None
    terminal: LedgerEvent | None = None


def _earliest(current: LedgerEvent | None, candidate: LedgerEvent) -> LedgerEvent:
    if current is None or candidate.ts < current.ts:
        return candidate
    return current


def _latest(current: LedgerEvent | None, candidate: LedgerEvent) -> LedgerEvent:
    if current is None or candidate.ts > current.ts:
        return candidate
    return current


def _collect(events: Iterable[LedgerEvent]) -> dict[str, _EnvRecord]:
    """Fold a stream of events into one `_EnvRecord` per environment name."""
    envs: dict[str, _EnvRecord] = {}
    for e in events:
        rec = envs.setdefault(e.env, _EnvRecord())
        if rec.persona is None and e.persona is not None:
            rec.persona = e.persona
        if e.event is EventType.REQUESTED:
            rec.requested = _earliest(rec.requested, e)
        elif e.event is EventType.READY:
            rec.ready = _earliest(rec.ready, e)
        elif e.event is EventType.FAILED:
            rec.failed = _earliest(rec.failed, e)
        elif e.event is EventType.EXPIRED:
            rec.expired = _earliest(rec.expired, e)
        elif e.event is EventType.EXTENDED:
            rec.last_extended = _latest(rec.last_extended, e)
        elif e.event in _TERMINAL_EVENTS:
            rec.terminal = _earliest(rec.terminal, e)
    return envs


def _provisioning_stats(envs: dict[str, _EnvRecord]) -> ProvisioningStats:
    """Provisioning seconds and phase timings for envs that reached Ready.

    Failed envs are excluded and counted in `.failed` instead. Envs that
    never had both `requested` and `ready` (still provisioning, or a `ready`
    with no matching `requested`) contribute nothing here.
    """
    seconds: list[float] = []
    timings_sum: dict[str, float] = {}
    timings_count: dict[str, int] = {}
    failed = 0
    for rec in envs.values():
        if rec.failed is not None:
            failed += 1
            continue
        if rec.requested is None or rec.ready is None:
            continue
        raw = rec.ready.details.get("provisioning_seconds")
        secs = float(raw) if raw is not None else (rec.ready.ts - rec.requested.ts).total_seconds()
        seconds.append(secs)
        for k, v in (rec.ready.details.get("timings") or {}).items():
            timings_sum[k] = timings_sum.get(k, 0.0) + float(v)
            timings_count[k] = timings_count.get(k, 0) + 1
    mean_timings = {k: timings_sum[k] / timings_count[k] for k in timings_sum}
    return ProvisioningStats(
        n=len(seconds),
        p50=_percentile(seconds, 50),
        p95=_percentile(seconds, 95),
        max=max(seconds) if seconds else 0.0,
        mean_timings=mean_timings,
        failed=failed,
    )


def _is_expired_env(rec: _EnvRecord) -> bool:
    """True if the env's teardown was TTL-driven, not a manual delete.

    Either it has an `expired` event (the operator saw the TTL pass), or its
    first terminal event is the sweeper reaping it for that same reason
    (`sweeper_reaped` with `details.kind == "reap_expired"` — the operator was
    down or never got to it).
    """
    if rec.expired is not None:
        return True
    if rec.terminal is not None and rec.terminal.event is EventType.SWEEPER_REAPED:
        return rec.terminal.details.get("kind") == "reap_expired"
    return False


def _expires_at_for(rec: _EnvRecord) -> datetime | None:
    """Best-effort reconstruction of the env's `expiresAt` from the ledger.

    Used only when the terminal event didn't already carry `lag_seconds`.
    Preferred source is the `expired` event's own `expires_at` (the
    authoritative status.expiresAt at the moment expiry fired); next, the most
    recent `extended` event's `new_expires_at`; finally `requested.ts + ttl`
    parsed from the `requested` event's `ttl` detail. Returns None if none of
    these can be recovered (e.g. a truncated ledger with no `ttl` recorded).
    """
    if rec.expired is not None:
        raw = rec.expired.details.get("expires_at")
        if raw:
            try:
                return from_rfc3339(str(raw))
            except ValueError:
                pass
    if rec.last_extended is not None:
        raw = rec.last_extended.details.get("new_expires_at")
        if raw:
            try:
                return from_rfc3339(str(raw))
            except ValueError:
                pass
    if rec.requested is not None:
        raw_ttl = rec.requested.details.get("ttl")
        if raw_ttl:
            try:
                return rec.requested.ts + parse_duration(str(raw_ttl))
            except InvalidDurationError:
                pass
    return None


def _lag_seconds(rec: _EnvRecord, env: str, warnings: list[str]) -> float | None:
    """Seconds between `expiresAt` and the terminal event, or None if undecidable."""
    assert rec.terminal is not None
    raw = rec.terminal.details.get("lag_seconds")
    if raw is not None:
        return float(raw)
    expires_at = _expires_at_for(rec)
    if expires_at is None:
        warnings.append(
            f"env {env!r}: no lag_seconds and expiresAt could not be derived from the "
            "ledger; excluded from the on-time count"
        )
        return None
    return (rec.terminal.ts - expires_at).total_seconds()


def _cleanup_stats(
    envs: dict[str, _EnvRecord], grace: timedelta, warnings: list[str]
) -> CleanupStats:
    """Reliability, lag percentiles, and sweeper/timeout counts (Ruling R15 dedup applied)."""
    reaped_by_sweeper = 0
    delete_timeouts = 0
    in_flight = 0
    expired = 0
    on_time = 0
    lags: list[float] = []
    for env, rec in envs.items():
        if rec.terminal is None:
            if rec.requested is not None:
                in_flight += 1
            continue
        if rec.terminal.event is EventType.SWEEPER_REAPED:
            reaped_by_sweeper += 1
        elif rec.terminal.event is EventType.DELETE_TIMEOUT:
            delete_timeouts += 1
        if not _is_expired_env(rec):
            continue
        expired += 1
        lag = _lag_seconds(rec, env, warnings)
        if lag is not None:
            lags.append(lag)
        if rec.terminal.event is EventType.DELETE_TIMEOUT:
            continue  # a timed-out finalizer never counts as on time
        if lag is not None and lag <= grace.total_seconds():
            on_time += 1
    return CleanupStats(
        expired=expired,
        on_time=on_time,
        reliability=(on_time / expired) if expired else 0.0,
        lag_p50=_percentile(lags, 50),
        lag_p95=_percentile(lags, 95),
        reaped_by_sweeper=reaped_by_sweeper,
        delete_timeouts=delete_timeouts,
        in_flight=in_flight,
    )


_MEMORY_UNITS: dict[str, float] = {"Gi": 1.0, "Mi": 1.0 / 1024, "Ki": 1.0 / (1024 * 1024)}


def _cpu_to_vcpu(cpu: str) -> float:
    """Kubernetes CPU quantity ("1", "500m") to vCPU count."""
    if cpu.endswith("m"):
        return float(cpu[:-1]) / 1000
    return float(cpu)


def _memory_to_gib(memory: str) -> float:
    """Kubernetes memory quantity ("1Gi", "512Mi") to GiB."""
    for suffix, factor in _MEMORY_UNITS.items():
        if memory.endswith(suffix):
            return float(memory[: -len(suffix)]) * factor
    return float(memory) / (1024**3)  # bare bytes, unlikely but not invalid


def _lifetime_hours(rec: _EnvRecord, window_end: datetime) -> float | None:
    """Hours from `requested` to the first terminal event, or to `window_end` if still alive."""
    if rec.requested is None:
        return None
    end = rec.terminal.ts if rec.terminal is not None else window_end
    return max((end - rec.requested.ts).total_seconds(), 0.0) / 3600


def _hourly_rate(persona: Persona, pricing: Pricing) -> float:
    return (
        _cpu_to_vcpu(persona.resources.cpu) * pricing.vcpu_hour_usd
        + _memory_to_gib(persona.resources.memory) * pricing.gib_hour_usd
    )


def _cost_stats(
    envs: dict[str, _EnvRecord],
    window_start: datetime,
    window_end: datetime,
    pricing: Pricing,
    personas: dict[str, Persona],
    warnings: list[str],
) -> CostStats:
    """On-demand cost of the real envs vs. one always-on env per persona (spec A7)."""
    on_demand_usd = 0.0
    seen_unknown: set[str] = set()
    for env, rec in envs.items():
        if rec.requested is None:
            continue
        persona_name = rec.persona
        persona = personas.get(persona_name) if persona_name is not None else None
        if persona is None:
            key = persona_name or "<none>"
            if key not in seen_unknown:
                seen_unknown.add(key)
                warnings.append(
                    f"env {env!r}: unknown persona {key!r}; excluded from the cost calculation"
                )
            continue
        hours = _lifetime_hours(rec, window_end)
        if hours is None:
            continue
        on_demand_usd += _hourly_rate(persona, pricing) * hours

    window_hours = (window_end - window_start).total_seconds() / 3600
    baseline_usd = sum(_hourly_rate(p, pricing) * window_hours for p in personas.values())
    savings_pct = ((baseline_usd - on_demand_usd) / baseline_usd * 100) if baseline_usd > 0 else 0.0
    return CostStats(
        on_demand_usd=on_demand_usd,
        baseline_usd=baseline_usd,
        savings_pct=savings_pct,
        window_hours=window_hours,
        pricing_source=pricing.source_url,
    )


def compute_metrics(
    events: Iterable[LedgerEvent],
    grace: timedelta = DEFAULT_GRACE,
    since: datetime | None = None,
    until: datetime | None = None,
    pricing: Pricing | None = None,
    personas: dict[str, Persona] | None = None,
) -> MetricsReport:
    """Compute the full metrics report from a ledger's events.

    `since`/`until` filter the events considered by every section (a
    truncated `datetime.now()`-anchored view, e.g. "the last day"). The cost
    window (Ruling R6) is `[since, until]` when given, falling back to the
    earliest/latest ts among the (already filtered) events on either side; the
    baseline assumes one always-on environment per persona in `personas` for
    that whole window. Cost is `None` when `pricing` or `personas` is missing,
    or when the window can't be established (no events and no explicit
    since/until).
    """
    filtered = [e for e in events if since is None or e.ts >= since]
    filtered = [e for e in filtered if until is None or e.ts <= until]
    envs = _collect(filtered)
    warnings: list[str] = []

    provisioning = _provisioning_stats(envs)
    cleanup = _cleanup_stats(envs, grace, warnings)

    window_start = since if since is not None else (min((e.ts for e in filtered), default=None))
    window_end = until if until is not None else (max((e.ts for e in filtered), default=None))
    cost: CostStats | None = None
    if (
        pricing is not None
        and personas is not None
        and window_start is not None
        and window_end is not None
    ):
        cost = _cost_stats(envs, window_start, window_end, pricing, personas, warnings)

    return MetricsReport(provisioning=provisioning, cleanup=cleanup, cost=cost, warnings=warnings)
