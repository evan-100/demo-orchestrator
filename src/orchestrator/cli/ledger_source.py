"""Copying the ledger out of the cluster for `democtl metrics`/`bench --from-cluster`.

Mirrors `tests/integration/conftest.InClusterOperator`: find the Running
operator pod by label selector in the `demo-orchestrator` namespace, then
`kubectl cp` its ledger file to a local temp file, falling back to
`kubectl exec ... cat` if `cp` isn't available (e.g. no `tar` in the image).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

OPERATOR_NAMESPACE = "demo-orchestrator"
OPERATOR_SELECTOR = "app.kubernetes.io/component=operator"
REMOTE_LEDGER_PATH = "/var/lib/orchestrator/ledger.jsonl"


class LedgerFetchError(RuntimeError):
    """Raised when the ledger can't be located or copied out of the cluster."""


def _kubectl(args: list[str], context: str | None) -> subprocess.CompletedProcess[bytes]:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += args
    return subprocess.run(cmd, capture_output=True, check=False)


def _find_operator_pod(context: str | None) -> str:
    result = _kubectl(
        [
            "-n",
            OPERATOR_NAMESPACE,
            "get",
            "pod",
            "-l",
            OPERATOR_SELECTOR,
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        context,
    )
    pod = result.stdout.decode(errors="replace").strip()
    if result.returncode != 0 or not pod:
        detail = result.stderr.decode(errors="replace").strip() or "no Running operator pod found"
        raise LedgerFetchError(
            f"could not find a Running operator pod in namespace {OPERATOR_NAMESPACE!r}: {detail}"
        )
    return pod


def fetch_cluster_ledger(context: str | None = None) -> Path:
    """Copy the ledger from the Running operator pod into a new temp file.

    Returns the local path; the caller is responsible for deleting it once
    done reading. Raises `LedgerFetchError` with a clear one-line message on
    any failure (no pod, `cp` and the `exec cat` fallback both failing).
    """
    pod = _find_operator_pod(context)

    fd, tmp_name = tempfile.mkstemp(prefix="democtl-ledger-", suffix=".jsonl")
    os.close(fd)
    dest = Path(tmp_name)

    cp_result = _kubectl(
        ["-n", OPERATOR_NAMESPACE, "cp", f"{pod}:{REMOTE_LEDGER_PATH}", str(dest)], context
    )
    if cp_result.returncode == 0:
        return dest

    exec_result = _kubectl(
        ["-n", OPERATOR_NAMESPACE, "exec", pod, "--", "cat", REMOTE_LEDGER_PATH], context
    )
    if exec_result.returncode != 0:
        dest.unlink(missing_ok=True)
        detail = (
            exec_result.stderr.decode(errors="replace").strip()
            or cp_result.stderr.decode(errors="replace").strip()
        )
        raise LedgerFetchError(f"failed to copy the ledger from pod {pod!r}: {detail}")
    dest.write_bytes(exec_result.stdout)
    return dest
