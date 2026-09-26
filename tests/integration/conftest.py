"""Fixtures for integration tests against the local kind cluster.

Requires `make cluster` (kind + ingress-nginx) and the `crewline:dev` image
loaded into kind. `ORCH_MODE` picks the operator under test:

- `local` (default): the operator runs as a subprocess against the current
  kubectl context. Refuses to start while an in-cluster operator is running,
  since two operators would fight over the same CRs.
- `incluster`: uses the operator deployed by `make up` / `make deploy`. No
  subprocess is started; the ledger is read with `kubectl cp` from its pod.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from orchestrator.constants import GROUP, PLURAL

REPO_ROOT = Path(__file__).resolve().parents[2]
CRD_PATH = REPO_ROOT / "deploy" / "crd" / "demoenvironments.yaml"
OPERATOR_COMMAND = [
    *("uv", "run", "kopf", "run"),
    *("-m", "orchestrator.operator.handlers", "--all-namespaces"),
]


ORCH_MODE = os.environ.get("ORCH_MODE", "local")
ORCH_NAMESPACE = "demo-orchestrator"
OPERATOR_SELECTOR = "app.kubernetes.io/component=operator"
IN_CLUSTER_LEDGER = "/var/lib/orchestrator/ledger.jsonl"


class Operator(Protocol):
    """The operator under test, wherever it runs."""

    @property
    def ledger_path(self) -> Path:
        """A local file holding the ledger as of this access."""
        ...

    def logs(self) -> str: ...


@dataclass(frozen=True)
class LocalOperator:
    process: subprocess.Popen[bytes]
    ledger_path: Path
    log_path: Path

    def logs(self) -> str:
        return self.log_path.read_text(errors="replace")


@dataclass(frozen=True)
class InClusterOperator:
    workdir: Path

    def _pod(self) -> str:
        return kubectl(
            *("-n", ORCH_NAMESPACE, "get", "pod", "-l", OPERATOR_SELECTOR),
            *("--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}"),
        )

    @property
    def ledger_path(self) -> Path:
        local = self.workdir / "ledger.jsonl"
        local.unlink(missing_ok=True)
        kubectl("-n", ORCH_NAMESPACE, "cp", f"{self._pod()}:{IN_CLUSTER_LEDGER}", str(local))
        return local

    def logs(self) -> str:
        return kubectl(
            *("-n", ORCH_NAMESPACE, "logs", "-l", OPERATOR_SELECTOR),
            *("--tail=-1", "--prefix"),
            check=False,
        )


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


def _running_operator_deployments() -> list[str]:
    out = kubectl(
        *("-n", ORCH_NAMESPACE, "get", "deploy", "-l", OPERATOR_SELECTOR),
        *("-o", "jsonpath={range .items[?(@.spec.replicas>0)]}{.metadata.name}{' '}{end}"),
        check=False,
    )
    return out.split()


@pytest.fixture(scope="session")
def crd() -> None:
    # In-cluster, the chart installed the CRD (from its crds/); only wait for it.
    if ORCH_MODE == "local":
        kubectl("apply", "--server-side", "-f", str(CRD_PATH))
    kubectl(
        "wait",
        "--for=condition=Established",
        f"crd/{PLURAL}.{GROUP}",
        "--timeout=30s",
    )


@pytest.fixture
def operator(crd: None, tmp_path: Path) -> Iterator[Operator]:
    if ORCH_MODE == "incluster":
        kubectl(
            *("-n", ORCH_NAMESPACE, "rollout", "status"),
            *("deploy/demo-orchestrator-operator", "--timeout=120s"),
        )
        yield InClusterOperator(workdir=tmp_path)
        return
    if ORCH_MODE != "local":
        pytest.fail(f"ORCH_MODE must be 'local' or 'incluster', not {ORCH_MODE!r}")
    if running := _running_operator_deployments():
        pytest.fail(
            f"in-cluster operator is running ({ORCH_NAMESPACE}: {', '.join(running)}); "
            "a local operator would fight it. Run with ORCH_MODE=incluster, or scale it "
            f"down: kubectl -n {ORCH_NAMESPACE} scale deploy --all --replicas=0"
        )
    yield from _local_operator(tmp_path)


def _local_operator(tmp_path: Path) -> Iterator[Operator]:
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
    op = LocalOperator(process=process, ledger_path=ledger_path, log_path=log_path)
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
