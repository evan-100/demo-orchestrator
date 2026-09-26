"""Unit tests for `democtl metrics`, driven through `typer.testing.CliRunner`.

Ledger fixtures are written by hand as JSONL (matching `LedgerEvent`'s wire
format) rather than going through `core.ledger.Ledger.append`, since the CLI
under test only ever reads a ledger file, never writes one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator.cli import ledger_source
from orchestrator.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = REPO_ROOT / "personas"

runner = CliRunner()


def _write_ledger(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def ledger_file(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.jsonl"
    _write_ledger(
        path,
        [
            {
                "ts": "2026-09-24T12:00:00.000Z",
                "event": "requested",
                "env": "healthcare-aaaa",
                "namespace": "demo-healthcare-aaaa",
                "persona": "healthcare",
                "actor": "cli",
                "details": {"ttl": "1h", "requestedBy": "evan"},
            },
            {
                "ts": "2026-09-24T12:00:30.000Z",
                "event": "ready",
                "env": "healthcare-aaaa",
                "namespace": "demo-healthcare-aaaa",
                "persona": "healthcare",
                "actor": "operator",
                "details": {"provisioning_seconds": 30},
            },
            {
                "ts": "2026-09-24T13:00:05.000Z",
                "event": "expired",
                "env": "healthcare-aaaa",
                "namespace": "demo-healthcare-aaaa",
                "persona": "healthcare",
                "actor": "operator",
                "details": {"expires_at": "2026-09-24T13:00:00.000Z"},
            },
            {
                "ts": "2026-09-24T13:00:05.000Z",
                "event": "deleted",
                "env": "healthcare-aaaa",
                "namespace": "demo-healthcare-aaaa",
                "persona": "healthcare",
                "actor": "operator",
                "details": {"lag_seconds": 5},
            },
        ],
    )
    return path


# --- table output ------------------------------------------------------------


def test_metrics_table_shows_expected_numbers(ledger_file: Path) -> None:
    result = runner.invoke(
        cli_main.app,
        ["metrics", "--ledger", str(ledger_file), "--pricing", str(REPO_ROOT / "pricing.yaml")],
    )

    assert result.exit_code == 0, result.output
    assert "Provisioning" in result.output
    assert "30.0" in result.output  # p50/p95/max all 30s for the one env
    assert "Cleanup" in result.output
    assert "100.0%" in result.output  # reliability: 1/1 on time
    assert "5.0" in result.output  # lag p50/p95
    assert "Cost" in result.output


# --- json output ---------------------------------------------------------


def test_metrics_json_has_stable_schema_keys(ledger_file: Path) -> None:
    result = runner.invoke(
        cli_main.app,
        [
            "metrics",
            "--ledger",
            str(ledger_file),
            "--pricing",
            str(REPO_ROOT / "pricing.yaml"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"provisioning", "cleanup", "cost", "warnings"}
    assert set(payload["provisioning"]) == {"n", "p50", "p95", "max", "mean_timings", "failed"}
    assert set(payload["cleanup"]) == {
        "expired",
        "on_time",
        "reliability",
        "lag_p50",
        "lag_p95",
        "reaped_by_sweeper",
        "delete_timeouts",
        "in_flight",
    }
    assert set(payload["cost"]) == {
        "on_demand_usd",
        "baseline_usd",
        "savings_pct",
        "window_hours",
        "pricing_source",
    }
    assert payload["provisioning"]["n"] == 1


# --- --since / --until parsing --------------------------------------------


@pytest.mark.parametrize("since_value", ["7d", "24h", "2026-09-24T00:00:00Z"])
def test_metrics_since_accepts_duration_and_iso(ledger_file: Path, since_value: str) -> None:
    result = runner.invoke(
        cli_main.app,
        [
            "metrics",
            "--ledger",
            str(ledger_file),
            "--pricing",
            str(REPO_ROOT / "pricing.yaml"),
            "--since",
            since_value,
        ],
    )

    assert result.exit_code == 0, result.output


def test_metrics_since_bad_value_exits_2(ledger_file: Path) -> None:
    result = runner.invoke(
        cli_main.app, ["metrics", "--ledger", str(ledger_file), "--since", "not-a-duration"]
    )

    assert result.exit_code == 2
    assert "invalid --since" in result.output


def test_metrics_until_bad_value_exits_2(ledger_file: Path) -> None:
    result = runner.invoke(
        cli_main.app, ["metrics", "--ledger", str(ledger_file), "--until", "not-a-timestamp"]
    )

    assert result.exit_code == 2
    assert "invalid --until" in result.output


# --- --from-cluster --------------------------------------------------------


def test_metrics_from_cluster_failure_is_one_line_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(context: str | None = None) -> Path:
        raise ledger_source.LedgerFetchError("no Running operator pod found")

    monkeypatch.setattr(cli_main, "fetch_cluster_ledger", boom)

    result = runner.invoke(cli_main.app, ["metrics", "--from-cluster"])

    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "no Running operator pod found" in lines[0]


def test_metrics_from_cluster_and_ledger_mutually_exclusive(ledger_file: Path) -> None:
    result = runner.invoke(
        cli_main.app, ["metrics", "--from-cluster", "--ledger", str(ledger_file)]
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
