"""`--since`/`--until` parsing and Rich/JSON rendering shared by `metrics` and `bench`.

Kept out of `cli.main` so that file stays a thin list of commands.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timedelta

from rich.console import Console
from rich.table import Table

from orchestrator.core.expiry import from_rfc3339
from orchestrator.core.metrics import MetricsReport
from orchestrator.core.pricing import Pricing

# Like `core.durations.parse_duration`'s grammar, but with a `d` (day) unit in
# front and no 8h cap: a metrics/bench window of "7d" or "30d" is normal,
# unlike a TTL. `core.durations.parse_duration` itself is not reused here for
# that reason (Ruling: don't loosen its cap for an unrelated purpose).
_SINCE_DURATION = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


class InvalidSinceError(ValueError):
    """Raised when `--since` is neither a valid duration nor an ISO8601 timestamp."""


def parse_since(text: str, now: datetime) -> datetime:
    """Parse `--since`: a duration ("7d"/"24h"/"30m") relative to `now`, or an ISO8601 timestamp."""
    match = _SINCE_DURATION.fullmatch(text)
    if match is not None and any(match.groups()):
        days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        if total_seconds > 0:
            return now - timedelta(seconds=total_seconds)
    try:
        return from_rfc3339(text)
    except ValueError:
        pass
    raise InvalidSinceError(
        f"invalid --since {text!r}: use a duration like 7d, 24h, 30m or an ISO8601 timestamp"
    )


def parse_until(text: str) -> datetime:
    """Parse `--until` as an ISO8601 timestamp."""
    try:
        return from_rfc3339(text)
    except ValueError as exc:
        raise InvalidSinceError(f"invalid --until {text!r}: {exc}") from exc


def report_to_dict(report: MetricsReport) -> dict[str, object]:
    """Convert a `MetricsReport` to a plain, JSON-stable dict."""
    return asdict(report)


def render_report(console: Console, report: MetricsReport, pricing: Pricing | None) -> None:
    """Print the Provisioning, Cleanup and Cost tables, then any warnings."""
    _render_provisioning(console, report)
    _render_cleanup(console, report)
    _render_cost(console, report, pricing)
    if report.warnings:
        console.print()
        console.print("[yellow]Warnings:[/yellow]")
        for warning in report.warnings:
            console.print(f"  - {warning}")


def _render_provisioning(console: Console, report: MetricsReport) -> None:
    p = report.provisioning
    table = Table(title="Provisioning")
    table.add_column("N")
    table.add_column("P50 (s)")
    table.add_column("P95 (s)")
    table.add_column("MAX (s)")
    table.add_column("FAILED")
    for key in sorted(p.mean_timings):
        table.add_column(key.upper())
    table.add_row(
        str(p.n),
        f"{p.p50:.1f}",
        f"{p.p95:.1f}",
        f"{p.max:.1f}",
        str(p.failed),
        *[f"{p.mean_timings[key]:.1f}" for key in sorted(p.mean_timings)],
    )
    console.print(table)


def _render_cleanup(console: Console, report: MetricsReport) -> None:
    c = report.cleanup
    table = Table(title="Cleanup")
    table.add_column("EXPIRED")
    table.add_column("ON TIME")
    table.add_column("RELIABILITY")
    table.add_column("LAG P50 (s)")
    table.add_column("LAG P95 (s)")
    table.add_column("REAPED BY SWEEPER")
    table.add_column("DELETE TIMEOUTS")
    table.add_column("IN FLIGHT")
    table.add_row(
        str(c.expired),
        str(c.on_time),
        f"{c.reliability * 100:.1f}%",
        f"{c.lag_p50:.1f}",
        f"{c.lag_p95:.1f}",
        str(c.reaped_by_sweeper),
        str(c.delete_timeouts),
        str(c.in_flight),
    )
    console.print(table)


def _render_cost(console: Console, report: MetricsReport, pricing: Pricing | None) -> None:
    table = Table(title="Cost")
    if report.cost is None:
        table.add_column("STATUS")
        table.add_row("no cost data (pricing or personas unavailable)")
        console.print(table)
        return
    cost = report.cost
    table.add_column("ON-DEMAND")
    table.add_column("BASELINE")
    table.add_column("SAVINGS")
    table.add_column("WINDOW (h)")
    table.add_column("PRICING SOURCE")
    table.add_column("RETRIEVED")
    table.add_row(
        f"${cost.on_demand_usd:.2f}",
        f"${cost.baseline_usd:.2f}",
        f"{cost.savings_pct:.1f}%",
        f"{cost.window_hours:.1f}",
        cost.pricing_source,
        str(pricing.retrieved) if pricing is not None else "-",
    )
    console.print(table)
