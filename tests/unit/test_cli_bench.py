"""CliRunner tests for `democtl bench`.

`BenchFakeKube` stands in for both the cluster *and* the operator: `create_env`
appends the `requested` ledger event a real operator would log, and `get_env`
resolves to Ready on the very first poll while also appending `ready`/
`deleted` events — so the happy path never needs a real sleep (both the
Ready-wait and the ledger terminal-event-wait resolve on their first poll).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestrator.cli import bench as bench_module
from orchestrator.cli import ledger_source
from orchestrator.cli import main as cli_main
from orchestrator.config import Settings
from orchestrator.core.expiry import to_rfc3339

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = REPO_ROOT / "personas"

runner = CliRunner()


class BenchFakeKube:
    """`create_env`/`get_env` that also plays the operator, writing to `ledger_path`."""

    def __init__(self, ledger_path: Path, *, emit_terminal: bool = True) -> None:
        self.ledger_path = ledger_path
        self.created: list[tuple[str, dict[str, Any]]] = []
        self._emit_terminal = emit_terminal

    def _append(self, event: str, env: str, persona: str, details: dict[str, object]) -> None:
        line = {
            "ts": to_rfc3339(cli_main.utcnow()),
            "event": event,
            "env": env,
            "namespace": f"demo-{env}",
            "persona": persona,
            "actor": "operator",
            "details": details,
        }
        with self.ledger_path.open("a") as f:
            f.write(json.dumps(line) + "\n")

    def create_env(self, name: str, spec: dict[str, Any]) -> None:
        self.created.append((name, dict(spec)))
        self._append("requested", name, spec["persona"], {"requestedBy": spec["requestedBy"]})

    def get_env(self, name: str) -> dict[str, Any] | None:
        if self._emit_terminal:
            self._append("ready", name, "healthcare", {"provisioning_seconds": 1})
            self._append("deleted", name, "healthcare", {"lag_seconds": 0})
        return {"status": {"phase": "Ready"}}


class ReadyKube:
    """A minimal `BenchKube` for tests that never touch the ledger's contents."""

    def create_env(self, name: str, spec: dict[str, Any]) -> None:
        pass

    def get_env(self, name: str) -> dict[str, Any] | None:
        return {"status": {"phase": "Ready"}}


@pytest.fixture
def bench_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(personas_dir=PERSONAS_DIR, ledger_path=tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    return settings


# --- usage errors ------------------------------------------------------------


def test_bench_n_not_positive_exits_2(bench_settings: Settings) -> None:
    result = runner.invoke(
        cli_main.app, ["bench", "--persona", "healthcare", "--n", "0", "--ttl", "2m"]
    )

    assert result.exit_code == 2
    assert "positive integer" in result.output


def test_bench_parallel_not_positive_exits_2(bench_settings: Settings) -> None:
    result = runner.invoke(
        cli_main.app,
        ["bench", "--persona", "healthcare", "--n", "1", "--ttl", "2m", "--parallel", "0"],
    )

    assert result.exit_code == 2
    assert "positive integer" in result.output


def test_bench_from_cluster_and_ledger_mutually_exclusive(
    bench_settings: Settings, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli_main.app,
        [
            "bench",
            "--persona",
            "healthcare",
            "--n",
            "1",
            "--ttl",
            "2m",
            "--from-cluster",
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


# --- --from-cluster failure --------------------------------------------------


def test_bench_ledger_fetch_error_exits_1(
    bench_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "get_kube", lambda context=None: ReadyKube())

    def boom(context: str | None = None) -> Path:
        raise ledger_source.LedgerFetchError("no Running operator pod found")

    monkeypatch.setattr(cli_main, "fetch_cluster_ledger", boom)

    result = runner.invoke(
        cli_main.app,
        ["bench", "--persona", "healthcare", "--n", "1", "--ttl", "2m", "--from-cluster"],
    )

    assert result.exit_code == 1
    assert "no Running operator pod found" in result.output


# --- happy path ---------------------------------------------------------


def test_bench_happy_path_exits_0_and_prints_tables(
    bench_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    kube = BenchFakeKube(bench_settings.ledger_path, emit_terminal=True)
    monkeypatch.setattr(cli_main, "get_kube", lambda context=None: kube)

    result = runner.invoke(
        cli_main.app, ["bench", "--persona", "healthcare", "--n", "1", "--ttl", "2m"]
    )

    assert result.exit_code == 0, result.output
    assert len(kube.created) == 1
    assert "Provisioning" in result.output
    assert "Cleanup" in result.output
    assert "Cost" in result.output
    assert "ready=1 failed=0 n=1" in result.output


# --- unterminated envs ----------------------------------------------------


def test_bench_unterminated_env_exits_1(
    bench_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    kube = BenchFakeKube(bench_settings.ledger_path, emit_terminal=False)
    monkeypatch.setattr(cli_main, "get_kube", lambda context=None: kube)

    # The real `run_bench` uses real time.sleep/time.monotonic; wrap it so the
    # ledger-wait deadline is reached in a handful of calls instead of really
    # waiting `n * timeout_per_env` seconds (this test never touches the
    # cluster; it only speeds up an otherwise-real deadline check).
    real_run_bench = bench_module.run_bench
    fake_now = {"t": 0.0}

    def fake_clock() -> float:
        fake_now["t"] += 1.0
        return fake_now["t"]

    def fast_run_bench(*args: Any, **kwargs: Any) -> Any:
        kwargs["sleep"] = lambda _seconds: None
        kwargs["clock"] = fake_clock
        return real_run_bench(*args, **kwargs)

    monkeypatch.setattr(cli_main, "run_bench", fast_run_bench)

    result = runner.invoke(
        cli_main.app,
        [
            "bench",
            "--persona",
            "healthcare",
            "--n",
            "1",
            "--ttl",
            "2m",
            "--timeout-per-env",
            "1",
        ],
    )

    assert result.exit_code == 1
    assert "never reached a terminal event within the timeout" in result.output
    # The partial metrics were still printed before the failure.
    assert "Provisioning" in result.output
