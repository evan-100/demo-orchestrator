"""Unit tests for `cli.ledger_source.fetch_cluster_ledger`, with `subprocess.run` faked.

No real `kubectl`/cluster involved: `subprocess.run` is monkeypatched to a
queue of canned `CompletedProcess`/`TimeoutExpired` results, one per expected
`kubectl` invocation (pod discovery, then `cp`, then the `exec cat` fallback).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from orchestrator.cli import ledger_source
from orchestrator.cli.ledger_source import LedgerFetchError, fetch_cluster_ledger

POD_OK = (0, b"healthcare-operator-abc123\n", b"")


class FakeRun:
    """Pops one canned response per `subprocess.run` call, in call order."""

    def __init__(self, responses: list[tuple[int, bytes, bytes] | str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        response = self._responses.pop(0)
        if response == "timeout":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fixed_tempfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route `tempfile.mkstemp` to a known path so tests can assert on its lifecycle."""
    dest = tmp_path / "democtl-ledger-test.jsonl"

    def fake_mkstemp(prefix: str = "", suffix: str = "") -> tuple[int, str]:
        fd = os.open(str(dest), os.O_CREAT | os.O_WRONLY, 0o600)
        return fd, str(dest)

    monkeypatch.setattr(tempfile, "mkstemp", fake_mkstemp)
    return dest


# --- pod discovery -----------------------------------------------------------


def test_no_running_pod_raises_ledger_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run = FakeRun([(0, b"", b"")])  # `kubectl get pod` succeeds but selects nothing
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LedgerFetchError, match="no Running operator pod"):
        fetch_cluster_ledger()

    assert len(fake_run.calls) == 1


# --- kubectl cp success --------------------------------------------------


def test_kubectl_cp_success_returns_dest(
    monkeypatch: pytest.MonkeyPatch, fixed_tempfile: Path
) -> None:
    fake_run = FakeRun([POD_OK, (0, b"", b"")])  # get pod, then cp
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch_cluster_ledger()

    assert result == fixed_tempfile
    assert len(fake_run.calls) == 2
    assert "cp" in fake_run.calls[1]
    fixed_tempfile.unlink(missing_ok=True)


# --- cp failure falls back to exec cat --------------------------------------


def test_cp_failure_falls_back_to_exec_cat(
    monkeypatch: pytest.MonkeyPatch, fixed_tempfile: Path
) -> None:
    fake_run = FakeRun(
        [
            POD_OK,
            (1, b"", b"error: tar not found"),  # cp fails
            (0, b'{"event": "requested"}\n', b""),  # exec cat succeeds
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch_cluster_ledger()

    assert result == fixed_tempfile
    assert fixed_tempfile.read_bytes() == b'{"event": "requested"}\n'
    assert len(fake_run.calls) == 3
    fixed_tempfile.unlink(missing_ok=True)


# --- both cp and exec fail ---------------------------------------------------


def test_cp_and_exec_both_fail_cleans_up_temp_file(
    monkeypatch: pytest.MonkeyPatch, fixed_tempfile: Path
) -> None:
    fake_run = FakeRun(
        [
            POD_OK,
            (1, b"", b"cp failed: no tar"),
            (1, b"", b"exec failed: pod terminating"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LedgerFetchError, match="failed to copy the ledger"):
        fetch_cluster_ledger()

    assert not fixed_tempfile.exists()


# --- kubectl timeouts ------------------------------------------------------


def test_kubectl_timeout_during_pod_discovery_raises_ledger_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run = FakeRun(["timeout"])
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LedgerFetchError, match="timed out"):
        fetch_cluster_ledger()


def test_kubectl_timeout_during_cp_cleans_up_temp_file(
    monkeypatch: pytest.MonkeyPatch, fixed_tempfile: Path
) -> None:
    fake_run = FakeRun([POD_OK, "timeout"])
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LedgerFetchError, match="timed out"):
        fetch_cluster_ledger()

    assert not fixed_tempfile.exists()


def test_kubectl_run_passes_a_timeout_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LedgerFetchError):
        fetch_cluster_ledger()

    assert captured.get("timeout") == ledger_source.KUBECTL_TIMEOUT_SECONDS
