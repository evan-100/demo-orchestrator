"""End-to-end: a DemoEnvironment CR becomes a Ready, persona-seeded Crewline app."""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import Operator, kubectl

from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.naming import generate_env_name, namespace_for

READY_TIMEOUT_SECONDS = 180
NAMESPACE_GONE_TIMEOUT_SECONDS = 120


def _get_env(name: str) -> dict[str, Any]:
    return json.loads(kubectl("get", "demoenvironment", name, "-o", "json"))


def _wait_namespace_gone(namespace: str) -> None:
    deadline = time.monotonic() + NAMESPACE_GONE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not kubectl("get", "namespace", namespace, "--ignore-not-found", "-o", "name"):
            return
        time.sleep(2)
    raise AssertionError(f"namespace {namespace} still present after CR deletion")


@pytest.fixture
def demo_env(operator: Operator) -> Iterator[str]:
    name = generate_env_name("healthcare")
    manifest = {
        "apiVersion": "orchestrator.local/v1alpha1",
        "kind": "DemoEnvironment",
        "metadata": {"name": name},
        "spec": {"persona": "healthcare", "ttl": "10m", "requestedBy": "integration-test"},
    }
    kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
    try:
        yield name
    finally:
        kubectl("delete", "demoenvironment", name, "--ignore-not-found", "--wait=true")
        _wait_namespace_gone(namespace_for(name))


@pytest.mark.integration
def test_provision_reaches_ready_and_serves_persona(operator: Operator, demo_env: str) -> None:
    name = demo_env
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = _get_env(name).get("status") or {}
        if status.get("phase") in ("Ready", "Failed"):
            break
        time.sleep(2)
    assert status.get("phase") == "Ready", f"status={status}\noperator log:\n{operator.logs()}"

    assert status["url"] == f"http://{name}.demo.localtest.me"
    assert status["namespace"] == namespace_for(name)
    assert set(status["timings"]) == {
        "namespaceSeconds",
        "appReadySeconds",
        "seedSeconds",
        "totalSeconds",
    }
    annotations = json.loads(kubectl("get", "namespace", status["namespace"], "-o", "json"))[
        "metadata"
    ]["annotations"]
    assert annotations["orchestrator.local/expires-at"] == status["expiresAt"]

    with urllib.request.urlopen(f"{status['url']}/people", timeout=10) as response:
        assert response.status == 200
        body = response.read().decode()
    assert "Riverbend Clinics" in body

    events = [e for e in Ledger(operator.ledger_path).read() if e.env == name]
    kinds = [e.event for e in events]
    assert kinds.count(EventType.REQUESTED) == 1
    assert kinds.count(EventType.READY) == 1
    ready = next(e for e in events if e.event == EventType.READY)
    assert ready.details["provisioning_seconds"] > 0
