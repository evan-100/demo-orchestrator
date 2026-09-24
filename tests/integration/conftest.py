"""Fixtures for integration tests against the local kind cluster.

Requires `make cluster` (kind + ingress-nginx) and the `crewline:dev` image
loaded into kind. The operator runs locally as a subprocess against the
current kubectl context.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from orchestrator.constants import GROUP, PLURAL

REPO_ROOT = Path(__file__).resolve().parents[2]
CRD_PATH = REPO_ROOT / "deploy" / "crd" / "demoenvironments.yaml"
OPERATOR_COMMAND = [
    *("uv", "run", "kopf", "run"),
    *("-m", "orchestrator.operator.handlers", "--all-namespaces"),
]


@dataclass(frozen=True)
class Operator:
    process: subprocess.Popen[bytes]
    ledger_path: Path
    log_path: Path

    def logs(self) -> str:
        return self.log_path.read_text(errors="replace")


def kubectl(*args: str, input_text: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture(scope="session")
def crd() -> None:
    kubectl("apply", "--server-side", "-f", str(CRD_PATH))
    kubectl(
        "wait",
        "--for=condition=Established",
        f"crd/{PLURAL}.{GROUP}",
        "--timeout=30s",
    )


@pytest.fixture
def operator(crd: None, tmp_path: Path) -> Iterator[Operator]:
    ledger_path = tmp_path / "ledger.jsonl"
    log_path = tmp_path / "operator.log"
    env = {
        **os.environ,
        "LEDGER_PATH": str(ledger_path),
        "PERSONAS_DIR": str(REPO_ROOT / "personas"),
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            OPERATOR_COMMAND,
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    op = Operator(process=process, ledger_path=ledger_path, log_path=log_path)
    time.sleep(3)
    if process.poll() is not None:
        pytest.fail(f"operator exited early:\n{op.logs()}")
    try:
        yield op
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
