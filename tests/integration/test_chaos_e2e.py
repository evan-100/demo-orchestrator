"""Chaos end-to-end: operator outage, operator restart mid-provision, and the capacity limit.

These drive the operator deployed by `make up` (ORCH_MODE=incluster) and the live
sweeper CronJob, scaling, killing and redeploying the operator. They are slow
(minutes each) and marked `chaos` so CI can leave them out:
`pytest -m "integration and not chaos"`.

Each test restores what it changed (operator replicas, Helm values) in a
`finally`, and deletes the DemoEnvironments it created.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
import yaml
from conftest import OPERATOR_SELECTOR, ORCH_MODE, ORCH_NAMESPACE, REPO_ROOT, Operator, kubectl

from orchestrator.constants import GROUP, KIND, PLURAL, VERSION
from orchestrator.core.expiry import from_rfc3339, utcnow
from orchestrator.core.ledger import EventType, Ledger, LedgerEvent
from orchestrator.core.naming import generate_env_name, namespace_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.chaos,
    pytest.mark.skipif(
        ORCH_MODE != "incluster",
        reason="chaos tests drive the deployed operator and sweeper (ORCH_MODE=incluster)",
    ),
]

RESOURCE = f"{PLURAL}.{GROUP}"
OPERATOR_DEPLOYMENT = "deploy/demo-orchestrator-operator"
SWEEPER_CRONJOB = "cronjob/demo-orchestrator-sweeper"
SWEEPER_SELECTOR = "app.kubernetes.io/component=sweeper"
CHART_VALUES = REPO_ROOT / "charts" / "demo-orchestrator" / "values.yaml"

READY_TIMEOUT_SECONDS = 180
RESTART_READY_TIMEOUT_SECONDS = 240
ADMISSION_TIMEOUT_SECONDS = 60
# After the operator goes down: TTL (90 s) + sweep grace + a cron tick, the
# sweeper's 30 s finalizer wait and namespace termination.
OUTAGE_SLACK_SECONDS = 90
OPERATOR_SOAK_SECONDS = 20
CAPACITY_LIMIT = 2
FAILED_INSPECTION_WINDOW = timedelta(minutes=10)
CLOCK_SKEW = timedelta(seconds=5)


def _get_env(name: str) -> dict[str, Any] | None:
    out = kubectl("get", RESOURCE, name, "--ignore-not-found", "-o", "json")
    return json.loads(out) if out.strip() else None


def _status(name: str) -> dict[str, Any]:
    return (_get_env(name) or {}).get("status") or {}


def _namespace_exists(name: str) -> bool:
    return bool(kubectl("get", "namespace", name, "--ignore-not-found", "-o", "name").strip())


def _create(name: str, ttl: str) -> None:
    manifest = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": KIND,
        "metadata": {"name": name},
        "spec": {"persona": "healthcare", "ttl": ttl, "requestedBy": "chaos-test"},
    }
    kubectl("apply", "-f", "-", input_text=json.dumps(manifest))


def _wait_phase(
    name: str, phases: set[str], timeout: float, operator: Operator, poll: float = 2
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = _status(name)
        if status.get("phase") in phases:
            return status
        time.sleep(poll)
    pytest.fail(f"{name} not in {phases} after {timeout}s: {status}\n{operator.logs()}")


def _cleanup(name: str) -> None:
    kubectl("delete", RESOURCE, name, "--ignore-not-found", "--wait=true", "--timeout=180s")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and _namespace_exists(namespace_for(name)):
        time.sleep(2)


def _env_events(operator: Operator, name: str) -> list[LedgerEvent]:
    return [e for e in Ledger(operator.ledger_path).read() if e.env == name]


def _operator(*args: str, check: bool = True) -> str:
    return kubectl("-n", ORCH_NAMESPACE, *args, check=check)


def _scale_operator(replicas: int) -> None:
    _operator("scale", OPERATOR_DEPLOYMENT, f"--replicas={replicas}")


def _wait_operator_rollout() -> None:
    _operator("rollout", "status", OPERATOR_DEPLOYMENT, "--timeout=180s")


def _operator_pods() -> list[dict[str, Any]]:
    pods: list[dict[str, Any]] = json.loads(
        _operator("get", "pod", "-l", OPERATOR_SELECTOR, "-o", "json")
    )["items"]
    return pods


def _assert_operator_healthy() -> None:
    """One Ready operator pod that stays up with no restarts through a short soak."""
    _wait_operator_rollout()
    time.sleep(OPERATOR_SOAK_SECONDS)
    pods = _operator_pods()
    assert len(pods) == 1, [p["metadata"]["name"] for p in pods]
    pod = pods[0]
    assert pod["status"]["phase"] == "Running", pod["status"]
    ready = {c["type"]: c["status"] for c in pod["status"].get("conditions", [])}
    assert ready.get("Ready") == "True", pod["status"]
    for container in pod["status"]["containerStatuses"]:
        waiting = container.get("state", {}).get("waiting") or {}
        assert waiting.get("reason") != "CrashLoopBackOff", container
        assert container["restartCount"] == 0, container


def _sweeper_logs() -> str:
    return _operator("logs", "-l", SWEEPER_SELECTOR, "--tail=-1", "--prefix", check=False)


def _sweep_grace() -> timedelta:
    env = json.loads(
        _operator(
            "get",
            SWEEPER_CRONJOB,
            "-o",
            "jsonpath={.spec.jobTemplate.spec.template.spec.containers[0].env}",
        )
    )
    return timedelta(seconds=int(next(e["value"] for e in env if e["name"] == "SWEEP_GRACE")))


def _deployed_max_envs() -> str:
    env = json.loads(
        _operator(
            "get", OPERATOR_DEPLOYMENT, "-o", "jsonpath={.spec.template.spec.containers[0].env}"
        )
    )
    return str(next(e["value"] for e in env if e["name"] == "MAX_CONCURRENT_ENVS"))


def _make_deploy(helm_args: str = "") -> None:
    result = subprocess.run(
        ["make", "deploy", f"HELM_ARGS={helm_args}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=420,
    )
    if result.returncode != 0:
        raise RuntimeError(f"make deploy failed:\n{result.stdout}\n{result.stderr}")
    _wait_operator_rollout()


def _within(actual: datetime, earliest: datetime, latest: datetime) -> bool:
    return earliest - CLOCK_SKEW <= actual <= latest + CLOCK_SKEW


@pytest.fixture
def env_name(operator: Operator) -> Iterator[str]:
    name = generate_env_name("healthcare")
    try:
        yield name
    finally:
        _cleanup(name)


def test_sweeper_reaps_expired_env_while_operator_is_down(
    operator: Operator, env_name: str
) -> None:
    assert _operator("get", SWEEPER_CRONJOB, "-o", "jsonpath={.spec.suspend}") == "false", (
        "the sweeper CronJob is suspended; this test needs the live backstop"
    )
    name, namespace = env_name, namespace_for(env_name)
    _create(name, "90s")
    status = _wait_phase(name, {"Ready", "Failed"}, READY_TIMEOUT_SECONDS, operator)
    assert status["phase"] == "Ready", status
    expires_at = from_rfc3339(status["expiresAt"])

    try:
        _scale_operator(0)
        _operator("wait", "--for=delete", "pod", "-l", OPERATOR_SELECTOR, "--timeout=120s")
        # Otherwise the operator's own expiry could have handled it.
        assert utcnow() < expires_at, "operator was still up when the env expired"

        deadline = (
            time.monotonic()
            + (
                (expires_at - utcnow()) + _sweep_grace() + timedelta(seconds=OUTAGE_SLACK_SECONDS)
            ).total_seconds()
        )
        while time.monotonic() < deadline:
            if not _namespace_exists(namespace) and _get_env(name) is None:
                break
            time.sleep(5)
        else:
            pytest.fail(
                f"namespace or CR still present with the operator down: "
                f"namespace={_namespace_exists(namespace)} cr={_get_env(name) is not None}\n"
                f"sweeper log:\n{_sweeper_logs()}"
            )
    finally:
        _scale_operator(1)
        _wait_operator_rollout()

    _assert_operator_healthy()
    events = _env_events(operator, name)
    kinds = [e.event for e in events]
    # The operator was down for the whole teardown: the sweeper did all of it.
    assert kinds.count(EventType.SWEEPER_REAPED) == 1, kinds
    assert EventType.EXPIRED not in kinds, kinds
    assert EventType.DELETED not in kinds, kinds
    reaped = next(e for e in events if e.event == EventType.SWEEPER_REAPED)
    assert reaped.actor == "sweeper"
    assert reaped.details["kind"] == "reap_expired", reaped.details
    assert reaped.details["cr_existed"] is True, reaped.details
    assert reaped.details["finalizer_removed"] is True, reaped.details
    assert reaped.details["lag_seconds"] >= _sweep_grace().total_seconds(), reaped.details
    # The orphaned CR was finalized by the sweeper, not left for the restarted operator.
    assert _get_env(name) is None


def test_operator_restart_mid_provision_converges(operator: Operator, env_name: str) -> None:
    name = env_name
    _create(name, "10m")
    status = _wait_phase(name, {"Seeding", "Ready", "Failed"}, READY_TIMEOUT_SECONDS, operator, 0.5)
    assert status["phase"] == "Seeding", f"missed the Seeding window: {status}"

    old_pods = {p["metadata"]["name"] for p in _operator_pods()}
    _operator("delete", "pod", "-l", OPERATOR_SELECTOR, "--wait=true", "--timeout=120s")
    killed_at = utcnow()
    assert _status(name).get("phase") == "Seeding", "env became Ready before the operator died"

    status = _wait_phase(name, {"Ready", "Failed"}, RESTART_READY_TIMEOUT_SECONDS, operator)
    assert status["phase"] == "Ready", status
    _wait_operator_rollout()
    assert not old_pods & {p["metadata"]["name"] for p in _operator_pods()}

    events = _env_events(operator, name)
    kinds = [e.event for e in events]
    assert kinds.count(EventType.REQUESTED) == 1, kinds
    assert kinds.count(EventType.READY) == 1, kinds
    assert EventType.FAILED not in kinds, kinds
    ready = next(e for e in events if e.event == EventType.READY)
    # Written by the restarted operator, from checkpoints the old one persisted.
    assert ready.ts > killed_at - CLOCK_SKEW, (ready.ts, killed_at)
    assert set(status["checkpoints"]) == {
        "provisioningAt",
        "workloadsAppliedAt",
        "seedingAt",
        "seededAt",
    }
    assert from_rfc3339(status["checkpoints"]["seedingAt"]) < killed_at


def test_env_over_capacity_fails_and_stays_inspectable(operator: Operator) -> None:
    assert not json.loads(kubectl("get", RESOURCE, "-o", "json"))["items"], (
        "DemoEnvironments already exist; they would count against the capacity limit"
    )
    names = [generate_env_name("healthcare") for _ in range(CAPACITY_LIMIT + 1)]
    *admitted, extra = names
    default_max_envs = str(yaml.safe_load(CHART_VALUES.read_text())["config"]["maxConcurrentEnvs"])

    _make_deploy(f"--set config.maxConcurrentEnvs={CAPACITY_LIMIT}")
    try:
        assert _deployed_max_envs() == str(CAPACITY_LIMIT)
        try:
            # One at a time: every existing non-Failed CR counts, admitted or not.
            for name in admitted:
                _create(name, "10m")
                status = _wait_phase(
                    name, {"Provisioning", "Seeding", "Ready", "Failed"}, 60, operator, 1
                )
                assert status["phase"] != "Failed", status

            before = utcnow()
            _create(extra, "10m")
            status = _wait_phase(
                extra,
                {"Provisioning", "Seeding", "Ready", "Failed"},
                ADMISSION_TIMEOUT_SECONDS,
                operator,
                1,
            )
            after = utcnow()
            assert status["phase"] == "Failed", status
            assert (
                status["message"]
                == f"capacity: {CAPACITY_LIMIT}/{CAPACITY_LIMIT} environments in use"
            )
            # Normal expiry cleans it up after the inspection window.
            assert _within(
                from_rfc3339(status["expiresAt"]),
                before + FAILED_INSPECTION_WINDOW,
                after + FAILED_INSPECTION_WINDOW,
            ), (status["expiresAt"], before, after)
            assert not _namespace_exists(namespace_for(extra))

            for name in admitted:
                ready = _wait_phase(name, {"Ready", "Failed"}, READY_TIMEOUT_SECONDS, operator)
                assert ready["phase"] == "Ready", ready

            kinds = [e.event for e in _env_events(operator, extra)]
            assert kinds == [EventType.REQUESTED, EventType.FAILED], kinds
        finally:
            for name in names:
                _cleanup(name)
    finally:
        _make_deploy()
    assert _deployed_max_envs() == default_max_envs
