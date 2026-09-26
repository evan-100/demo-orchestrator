"""Unit tests for `core.metrics.compute_metrics` and `core.pricing.load_pricing`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.core.expiry import to_rfc3339
from orchestrator.core.ledger import EventType, LedgerEvent
from orchestrator.core.metrics import _cpu_to_vcpu, _memory_to_gib, compute_metrics
from orchestrator.core.personas import Persona
from orchestrator.core.pricing import Pricing, load_pricing

REPO_ROOT = Path(__file__).resolve().parents[2]

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def ev(
    ts: datetime,
    event: EventType,
    env: str,
    *,
    persona: str | None = "healthcare",
    actor: str = "operator",
    details: dict[str, object] | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        ts=ts,
        event=event,
        env=env,
        namespace=f"demo-{env}",
        persona=persona,
        actor=actor,  # type: ignore[arg-type]
        details=details or {},
    )


def make_persona(name: str, *, cpu: str = "1", memory: str = "1Gi") -> Persona:
    return Persona.model_validate(
        {
            "name": name,
            "display_name": name.title(),
            "brand": {"company_name": name.title(), "primary_color": "#112233"},
            "default_ttl": "1h",
            "max_ttl": "2h",
            "resources": {"cpu": cpu, "memory": memory, "pods": 3},
            "fixtures": {
                "seed": 1,
                "locations": 1,
                "employees": 1,
                "roles": ["role"],
                "certifications": ["cert"],
                "shift_pattern": "8h",
            },
        }
    )


@pytest.fixture
def pricing() -> Pricing:
    return Pricing(
        vcpu_hour_usd=0.05,
        gib_hour_usd=0.005,
        source_url="https://example.invalid/pricing",
        retrieved="2026-09-26",  # type: ignore[arg-type]
    )


@pytest.fixture
def personas() -> dict[str, Persona]:
    return {
        "healthcare": make_persona("healthcare"),
        "manufacturing": make_persona("manufacturing"),
        "restaurant": make_persona("restaurant"),
    }


# --- provisioning -------------------------------------------------------------


def test_provisioning_stats_p50_p95_max() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={"provisioning_seconds": 30}),
        ev(T0, EventType.REQUESTED, "e2"),
        ev(T0 + timedelta(seconds=40), EventType.READY, "e2", details={"provisioning_seconds": 40}),
        ev(T0, EventType.REQUESTED, "e3"),
        ev(T0 + timedelta(seconds=50), EventType.READY, "e3", details={"provisioning_seconds": 50}),
        ev(T0, EventType.REQUESTED, "e4"),
        ev(
            T0 + timedelta(seconds=100),
            EventType.READY,
            "e4",
            details={"provisioning_seconds": 100},
        ),
    ]
    report = compute_metrics(events)
    assert report.provisioning.n == 4
    assert report.provisioning.p50 == 40
    assert report.provisioning.p95 == 100
    assert report.provisioning.max == 100
    assert report.provisioning.failed == 0


def test_provisioning_mean_timings_averages_ready_details() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(
            T0 + timedelta(seconds=10),
            EventType.READY,
            "e1",
            details={
                "provisioning_seconds": 10,
                "timings": {"seedSeconds": 4.0, "totalSeconds": 10.0},
            },
        ),
        ev(T0, EventType.REQUESTED, "e2"),
        ev(
            T0 + timedelta(seconds=20),
            EventType.READY,
            "e2",
            details={
                "provisioning_seconds": 20,
                "timings": {"seedSeconds": 6.0, "totalSeconds": 20.0},
            },
        ),
    ]
    report = compute_metrics(events)
    assert report.provisioning.mean_timings == {"seedSeconds": 5.0, "totalSeconds": 15.0}


def test_provisioning_falls_back_to_ready_minus_requested_ts() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=17), EventType.READY, "e1"),
    ]
    report = compute_metrics(events)
    assert report.provisioning.n == 1
    assert report.provisioning.p50 == 17


def test_failed_envs_excluded_from_provisioning_and_counted_separately() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=10), EventType.READY, "e1", details={"provisioning_seconds": 10}),
        ev(T0, EventType.REQUESTED, "e2"),
        ev(
            T0 + timedelta(seconds=5), EventType.FAILED, "e2", details={"reason": "seed job failed"}
        ),
    ]
    report = compute_metrics(events)
    assert report.provisioning.n == 1
    assert report.provisioning.failed == 1


def test_requested_without_terminal_is_in_flight() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=10), EventType.READY, "e1", details={"provisioning_seconds": 10}),
    ]
    report = compute_metrics(events)
    assert report.cleanup.in_flight == 1
    assert report.cleanup.expired == 0


# --- cleanup --------------------------------------------------------------


def _expired_and_terminal(
    env: str,
    lag: float,
    *,
    terminal_event: EventType = EventType.DELETED,
    terminal_actor: str = "operator",
    terminal_details: dict[str, object] | None = None,
) -> list[LedgerEvent]:
    expires_at = T0 + timedelta(hours=1)
    terminal_ts = expires_at + timedelta(seconds=lag)
    details = {"lag_seconds": lag}
    if terminal_details:
        details.update(terminal_details)
    return [
        ev(T0, EventType.REQUESTED, env),
        ev(T0 + timedelta(seconds=30), EventType.READY, env, details={"provisioning_seconds": 30}),
        ev(
            expires_at,
            EventType.EXPIRED,
            env,
            details={"expires_at": to_rfc3339(expires_at), "previous_phase": "Ready"},
        ),
        ev(terminal_ts, terminal_event, env, actor=terminal_actor, details=details),
    ]


def test_cleanup_reliability_lags_and_sweeper_reaped() -> None:
    events = [
        *_expired_and_terminal("e1", 5),
        *_expired_and_terminal("e2", 60),
        *_expired_and_terminal(
            "e3",
            300,
            terminal_event=EventType.SWEEPER_REAPED,
            terminal_actor="sweeper",
            terminal_details={"kind": "reap_expired", "reason": "expired past grace"},
        ),
    ]
    report = compute_metrics(events)
    assert report.cleanup.expired == 3
    assert report.cleanup.on_time == 2
    assert report.cleanup.reliability == pytest.approx(2 / 3)
    assert report.cleanup.reaped_by_sweeper == 1
    assert report.cleanup.delete_timeouts == 0
    assert report.cleanup.lag_p50 == 60
    assert report.cleanup.lag_p95 == 300


def test_manual_delete_excluded_from_expired_denominator() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={"provisioning_seconds": 30}),
        ev(
            T0 + timedelta(minutes=5),
            EventType.DELETED,
            "e1",
            details={"lag_seconds": None, "reason": "manual"},
        ),
    ]
    report = compute_metrics(events)
    assert report.cleanup.expired == 0
    assert report.cleanup.on_time == 0
    assert report.cleanup.in_flight == 0
    assert report.cleanup.reliability == 0.0


def test_delete_timeout_terminal_counts_as_not_on_time() -> None:
    events = _expired_and_terminal(
        "e1",
        5,  # a small lag_seconds value would otherwise look "on time"
        terminal_event=EventType.DELETE_TIMEOUT,
        terminal_details={"elapsed_seconds": 120.0, "reason": "expired"},
    )
    report = compute_metrics(events)
    assert report.cleanup.expired == 1
    assert report.cleanup.on_time == 0
    assert report.cleanup.delete_timeouts == 1
    assert report.cleanup.reliability == 0.0


def test_duplicate_requested_dedups_to_first_by_ts() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=1), EventType.REQUESTED, "e1"),  # e.g. a retried reconcile
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={}),
    ]
    report = compute_metrics(events)
    assert report.provisioning.n == 1
    assert report.provisioning.p50 == 30  # measured from the first `requested`, not the second


def test_duplicate_deleted_dedups_to_first_by_ts() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={"provisioning_seconds": 30}),
        ev(
            T0 + timedelta(hours=1),
            EventType.EXPIRED,
            "e1",
            details={"expires_at": to_rfc3339(T0 + timedelta(hours=1))},
        ),
        ev(T0 + timedelta(hours=1, seconds=5), EventType.DELETED, "e1", details={"lag_seconds": 5}),
        ev(
            T0 + timedelta(hours=1, seconds=999),
            EventType.DELETED,
            "e1",
            details={"lag_seconds": 999},
        ),
    ]
    report = compute_metrics(events)
    assert report.cleanup.expired == 1
    assert (
        report.cleanup.on_time == 1
    )  # the first `deleted` (lag 5) wins, not the duplicate (lag 999)
    assert report.cleanup.lag_p50 == 5


def test_deleted_then_sweeper_reaped_keeps_only_first_terminal() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={"provisioning_seconds": 30}),
        ev(
            T0 + timedelta(hours=1),
            EventType.EXPIRED,
            "e1",
            details={"expires_at": to_rfc3339(T0 + timedelta(hours=1))},
        ),
        ev(T0 + timedelta(hours=1, seconds=5), EventType.DELETED, "e1", details={"lag_seconds": 5}),
        ev(
            T0 + timedelta(hours=1, seconds=999),
            EventType.SWEEPER_REAPED,
            "e1",
            actor="sweeper",
            details={"lag_seconds": 999, "kind": "reap_expired"},
        ),
    ]
    report = compute_metrics(events)
    assert report.cleanup.expired == 1
    assert report.cleanup.reaped_by_sweeper == 0  # the operator's `deleted` was first
    assert report.cleanup.on_time == 1
    assert report.cleanup.lag_p50 == 5


def test_lag_derived_from_expired_details_when_lag_seconds_missing() -> None:
    expires_at = T0 + timedelta(hours=1)
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={"provisioning_seconds": 30}),
        ev(expires_at, EventType.EXPIRED, "e1", details={"expires_at": to_rfc3339(expires_at)}),
        # No lag_seconds recorded (e.g. an older ledger entry): derive from expiresAt.
        ev(
            expires_at + timedelta(seconds=15),
            EventType.DELETED,
            "e1",
            details={"reason": "expired"},
        ),
    ]
    report = compute_metrics(events)
    assert report.cleanup.expired == 1
    assert report.cleanup.on_time == 1
    assert report.cleanup.lag_p50 == 15


def test_undecidable_expiry_excluded_from_on_time_with_warning() -> None:
    events = [
        ev(T0, EventType.REQUESTED, "e1", details={}),  # no ttl recorded
        ev(T0 + timedelta(seconds=30), EventType.READY, "e1", details={"provisioning_seconds": 30}),
        ev(T0 + timedelta(hours=1), EventType.EXPIRED, "e1", details={"previous_phase": "Ready"}),
        # No lag_seconds and no derivable expiresAt (no expires_at/extended/ttl).
        ev(
            T0 + timedelta(hours=1, seconds=15),
            EventType.DELETED,
            "e1",
            details={"reason": "expired"},
        ),
    ]
    report = compute_metrics(events)
    assert report.cleanup.expired == 1
    assert report.cleanup.on_time == 0
    assert report.warnings  # the "cannot derive expiresAt" case is noted, not silently dropped


# --- resource quantity parsing ------------------------------------------------


@pytest.mark.parametrize(
    ("cpu", "expected"),
    [("500m", 0.5), ("250m", 0.25), ("0.5", 0.5), ("1", 1.0), ("2", 2.0)],
)
def test_cpu_to_vcpu(cpu: str, expected: float) -> None:
    assert _cpu_to_vcpu(cpu) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        ("512Mi", 0.5),
        ("2Gi", 2.0),
        ("1G", 0.9313225746154785),
        ("1024Mi", 1.0),
        ("2Ti", 2048.0),
    ],
)
def test_memory_to_gib(memory: str, expected: float) -> None:
    assert _memory_to_gib(memory) == pytest.approx(expected)


def test_cpu_and_memory_unparseable_return_none_not_exception() -> None:
    assert _cpu_to_vcpu("lots") is None
    assert _memory_to_gib("lots") is None


def test_bad_persona_quantity_skips_with_warning_not_exception(
    pricing: Pricing, personas: dict[str, Persona]
) -> None:
    since = T0
    until = T0 + timedelta(hours=1)
    broken = dict(personas)
    broken["healthcare"] = make_persona("healthcare", memory="not-a-quantity")
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(until, EventType.DELETED, "e1", details={"lag_seconds": 0, "reason": "manual"}),
    ]
    report = compute_metrics(events, since=since, until=until, pricing=pricing, personas=broken)
    assert report.cost is not None  # never crashes
    assert any("healthcare" in w for w in report.warnings)
    # The other two (valid) personas still contribute to the baseline.
    assert report.cost.baseline_usd == pytest.approx(0.055 * 2)


# --- cost -------------------------------------------------------------------


def test_cost_on_demand_two_envs_two_hours(pricing: Pricing, personas: dict[str, Persona]) -> None:
    # Two 1-vCPU/1-GiB envs, each alive for 2h: 2 * 2 * (0.05 + 0.005) = 0.22.
    since = T0
    until = T0 + timedelta(hours=2)
    events = [
        ev(T0, EventType.REQUESTED, "e1"),
        ev(until, EventType.DELETED, "e1", details={"lag_seconds": 0, "reason": "manual"}),
        ev(T0, EventType.REQUESTED, "e2"),
        ev(until, EventType.DELETED, "e2", details={"lag_seconds": 0, "reason": "manual"}),
    ]
    report = compute_metrics(events, since=since, until=until, pricing=pricing, personas=personas)
    assert report.cost is not None
    assert report.cost.on_demand_usd == pytest.approx(0.22)
    assert report.cost.window_hours == pytest.approx(2.0)
    assert report.cost.pricing_source == pricing.source_url


def test_cost_baseline_three_personas_24h(pricing: Pricing, personas: dict[str, Persona]) -> None:
    since = T0
    until = T0 + timedelta(hours=24)
    events = [ev(T0, EventType.REQUESTED, "e1")]  # any activity, just to have events in the window
    report = compute_metrics(events, since=since, until=until, pricing=pricing, personas=personas)
    assert report.cost is not None
    assert report.cost.baseline_usd == pytest.approx(3.96)
    assert report.cost.savings_pct == pytest.approx(
        (report.cost.baseline_usd - report.cost.on_demand_usd) / report.cost.baseline_usd * 100
    )


def test_cost_none_without_pricing_or_personas() -> None:
    events = [ev(T0, EventType.REQUESTED, "e1")]
    assert compute_metrics(events).cost is None
    assert compute_metrics(events, pricing=None).cost is None


def test_cost_unknown_persona_skipped_with_warning(
    pricing: Pricing, personas: dict[str, Persona]
) -> None:
    since = T0
    until = T0 + timedelta(hours=2)
    events = [
        ev(T0, EventType.REQUESTED, "ghost", persona="does-not-exist"),
        ev(until, EventType.DELETED, "ghost", details={"lag_seconds": 0, "reason": "manual"}),
    ]
    report = compute_metrics(events, since=since, until=until, pricing=pricing, personas=personas)
    assert report.cost is not None
    assert report.cost.on_demand_usd == 0.0
    assert any("does-not-exist" in w for w in report.warnings)


def test_cost_env_started_before_since_billed_only_for_window_portion(
    pricing: Pricing, personas: dict[str, Persona]
) -> None:
    """Ruling R20: `since` must not drop an env's pre-window cost to zero."""
    since = T0 + timedelta(hours=1)
    until = T0 + timedelta(hours=3)
    events = [
        ev(T0, EventType.REQUESTED, "e1"),  # requested an hour before the window opens
        ev(
            T0 + timedelta(hours=2),
            EventType.DELETED,
            "e1",
            details={"lag_seconds": 0, "reason": "manual"},
        ),
    ]
    report = compute_metrics(events, since=since, until=until, pricing=pricing, personas=personas)
    assert report.cost is not None
    # Billed only for [since, deleted] = 1h, not the full [requested, deleted] = 2h.
    assert report.cost.on_demand_usd == pytest.approx(0.055 * 1)


# --- since/until filtering and empty ledger ----------------------------------


def test_since_filters_out_earlier_events() -> None:
    since = T0 + timedelta(hours=1)
    events = [
        ev(T0, EventType.REQUESTED, "old"),
        ev(
            T0 + timedelta(seconds=10), EventType.READY, "old", details={"provisioning_seconds": 10}
        ),
        ev(since, EventType.REQUESTED, "new"),
        ev(
            since + timedelta(seconds=20),
            EventType.READY,
            "new",
            details={"provisioning_seconds": 20},
        ),
    ]
    report = compute_metrics(events, since=since)
    assert report.provisioning.n == 1
    assert report.provisioning.p50 == 20


def test_empty_ledger_reports_zeros_without_zero_division(
    pricing: Pricing, personas: dict[str, Persona]
) -> None:
    report = compute_metrics([], pricing=pricing, personas=personas)
    assert report.provisioning.n == 0
    assert report.provisioning.p50 == 0
    assert report.provisioning.p95 == 0
    assert report.provisioning.max == 0
    assert report.provisioning.mean_timings == {}
    assert report.cleanup.expired == 0
    assert report.cleanup.reliability == 0.0
    assert report.cleanup.lag_p50 == 0
    assert report.cleanup.lag_p95 == 0
    assert report.cleanup.in_flight == 0
    # No events at all means no window can be derived, so there's nothing to cost.
    assert report.cost is None


# --- pricing ------------------------------------------------------------------


def test_load_pricing_reads_repo_pricing_yaml() -> None:
    loaded = load_pricing(REPO_ROOT / "pricing.yaml")
    # Pin the ruled GKE Autopilot on-demand list prices exactly, not just > 0,
    # so an accidental edit to pricing.yaml fails this test instead of
    # silently changing every cost figure downstream.
    assert loaded.vcpu_hour_usd == 0.0445
    assert loaded.gib_hour_usd == 0.0049225
    assert loaded.source_url.startswith("https://")
    assert loaded.region
