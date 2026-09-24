"""End-to-end: TTL expiry tears a DemoEnvironment down, and extending it moves the expiry."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import Operator, kubectl

from orchestrator.constants import ANNOTATION_EXPIRES_AT, GROUP, KIND, PLURAL, VERSION
from orchestrator.core.expiry import from_rfc3339
from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.naming import generate_env_name, namespace_for

READY_TIMEOUT_SECONDS = 180
TEARDOWN_TIMEOUT_SECONDS = 90 + 120
ANNOTATION_TIMEOUT_SECONDS = 30
RESOURCE = f"{PLURAL}.{GROUP}"


def _get_env(name: str) -> dict[str, Any] | None:
    out = kubectl("get", RESOURCE, name, "--ignore-not-found", "-o", "json")
    return json.loads(out) if out.strip() else None


def _namespace(name: str) -> dict[str, Any] | None:
    out = kubectl("get", "namespace", name, "--ignore-not-found", "-o", "json")
    return json.loads(out) if out.strip() else None


def _create(name: str, ttl: str) -> None:
    manifest = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": KIND,
        "metadata": {"name": name},
        "spec": {"persona": "healthcare", "ttl": ttl, "requestedBy": "integration-test"},
    }
    kubectl("apply", "-f", "-", input_text=json.dumps(manifest))


def _wait_ready(name: str, operator: Operator) -> dict[str, Any]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        env = _get_env(name)
        status = (env or {}).get("status") or {}
        if status.get("phase") in ("Ready", "Failed"):
            break
        time.sleep(2)
    assert status.get("phase") == "Ready", f"status={status}\noperator log:\n{operator.logs()}"
    return status


def _cleanup(name: str) -> None:
    kubectl("delete", RESOURCE, name, "--ignore-not-found", "--wait=true", "--timeout=180s")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and _namespace(namespace_for(name)) is not None:
        time.sleep(2)


@pytest.fixture
def env_name(operator: Operator) -> Iterator[str]:
    name = generate_env_name("healthcare")
    try:
        yield name
    finally:
        _cleanup(name)


@pytest.mark.integration
def test_ttl_expiry_deletes_cr_and_namespace(operator: Operator, env_name: str) -> None:
    name, namespace = env_name, namespace_for(env_name)
    _create(name, "90s")
    status = _wait_ready(name, operator)
    assert status["namespace"] == namespace

    deadline = time.monotonic() + TEARDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _get_env(name) is None and _namespace(namespace) is None:
            break
        time.sleep(3)
    else:
        pytest.fail(f"CR or namespace still present\noperator log:\n{operator.logs()}")

    events = [e for e in Ledger(operator.ledger_path).read() if e.env == name]
    kinds = [e.event for e in events]
    assert kinds.index(EventType.EXPIRED) < kinds.index(EventType.DELETED), kinds
    assert EventType.DELETE_TIMEOUT not in kinds
    deleted = next(e for e in events if e.event == EventType.DELETED)
    assert deleted.details["reason"] == "expired"
    assert 0 <= deleted.details["lag_seconds"] <= 120


@pytest.mark.integration
def test_extending_ttl_moves_namespace_annotation(operator: Operator, env_name: str) -> None:
    name, namespace = env_name, namespace_for(env_name)
    _create(name, "5m")
    status = _wait_ready(name, operator)
    ns = _namespace(namespace)
    assert ns is not None
    before = from_rfc3339(ns["metadata"]["annotations"][ANNOTATION_EXPIRES_AT])
    assert before == from_rfc3339(status["expiresAt"])

    kubectl("patch", RESOURCE, name, "--type=merge", "-p", json.dumps({"spec": {"ttl": "6m"}}))

    deadline = time.monotonic() + ANNOTATION_TIMEOUT_SECONDS
    after = before
    while time.monotonic() < deadline:
        ns = _namespace(namespace) or {}
        after = from_rfc3339(ns["metadata"]["annotations"][ANNOTATION_EXPIRES_AT])
        if after != before:
            break
        time.sleep(1)
    assert (after - before).total_seconds() == 60, f"operator log:\n{operator.logs()}"

    env = _get_env(name)
    assert env is not None
    assert from_rfc3339(env["status"]["expiresAt"]) == after
    events = [e for e in Ledger(operator.ledger_path).read() if e.env == name]
    extended = [e for e in events if e.event == EventType.EXTENDED]
    assert len(extended) == 1
    assert from_rfc3339(extended[0].details["new_expires_at"]) == after
