"""Unit tests for the provisioning logic, driven through a fake Kubernetes facade."""

from __future__ import annotations

import copy
import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import kopf
import pytest
import urllib3
import yaml
from kubernetes.client.exceptions import ApiException

from orchestrator.constants import ANNOTATION_EXPIRES_AT, LABEL_ENV
from orchestrator.core.expiry import from_rfc3339, to_rfc3339
from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.personas import Persona, load_personas
from orchestrator.operator import handlers, provision
from orchestrator.operator.provision import (
    AdmissionError,
    ProvisionDeps,
    check_admission,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = REPO_ROOT / "personas"

NAME = "healthcare-ab12"
NAMESPACE = "demo-healthcare-ab12"
UID = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
CREATED = datetime(2026, 9, 24, 15, 0, 0, tzinfo=UTC)
CREATION_TIMESTAMP = "2026-09-24T15:00:00Z"


class FakeKube:
    """In-memory stand-in for `KubeFacade`."""

    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.available: set[tuple[str, str]] = set()
        self.job: Literal["running", "succeeded", "failed"] = "running"
        self.active = 0
        self.active_excluded: list[str] = []
        self.namespace_annotations: dict[str, dict[str, str]] = {}

    def apply(self, manifest: dict[str, Any]) -> None:
        self.applied.append(copy.deepcopy(manifest))
        if manifest["kind"] == "Namespace":
            name = manifest["metadata"]["name"]
            annotations = dict(manifest["metadata"].get("annotations", {}))
            self.namespace_annotations.setdefault(name, {}).update(annotations)

    def deployment_available(self, ns: str, name: str) -> bool:
        return (ns, name) in self.available

    def job_status(self, ns: str, name: str) -> Literal["running", "succeeded", "failed"]:
        return self.job

    def count_active_envs(self, exclude: str) -> int:
        self.active_excluded.append(exclude)
        return self.active

    def set_namespace_expiry(self, ns: str, expires_at: str) -> None:
        if ns in self.namespace_annotations:
            self.namespace_annotations[ns][ANNOTATION_EXPIRES_AT] = expires_at

    def kinds_applied(self) -> list[str]:
        return [m["kind"] for m in self.applied]


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def personas() -> dict[str, Persona]:
    return load_personas(PERSONAS_DIR)


@pytest.fixture
def kube() -> FakeKube:
    return FakeKube()


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.jsonl")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(CREATED + timedelta(seconds=1))
    monkeypatch.setattr(provision, "utcnow", clock)
    return clock


@pytest.fixture
def deps(kube: FakeKube, ledger: Ledger, personas: dict[str, Persona]) -> ProvisionDeps:
    return ProvisionDeps(
        kube=kube,
        ledger=ledger,
        personas=personas,
        max_envs=5,
        provision_timeout=timedelta(seconds=300),
        base_domain="demo.localtest.me",
    )


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Apply `patch` to `target` the way a JSON merge patch would."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class Env:
    """Drives repeated reconcile passes, persisting status between them like the API server."""

    def __init__(self, deps: ProvisionDeps, spec: dict[str, Any] | None = None) -> None:
        self.deps = deps
        self.spec = spec or {"persona": "healthcare", "ttl": "10m"}
        self.status: dict[str, Any] = {}

    def run(self) -> None:
        patch: dict[str, Any] = {}
        try:
            provision.provision(
                name=NAME,
                uid=UID,
                spec=self.spec,
                status=copy.deepcopy(self.status),
                creation_timestamp=CREATION_TIMESTAMP,
                patch_status=patch,
                deps=self.deps,
            )
        finally:
            _merge(self.status, patch)

    def admit(self) -> None:
        """Run the first pass, which only admits and persists the request."""
        with pytest.raises(kopf.TemporaryError) as exc:
            self.run()
        assert exc.value.delay == 1


def _events(ledger: Ledger) -> list[EventType]:
    return [e.event for e in ledger.read()]


# --- check_admission ---------------------------------------------------------


def test_admission_accepts_valid_request(personas: dict[str, Persona]) -> None:
    persona, ttl = check_admission({"persona": "healthcare", "ttl": "1h30m"}, personas, 0, 5)
    assert persona.name == "healthcare"
    assert ttl == timedelta(hours=1, minutes=30)


def test_admission_unknown_persona_lists_valid_personas(personas: dict[str, Persona]) -> None:
    with pytest.raises(AdmissionError) as exc:
        check_admission({"persona": "banking", "ttl": "1h"}, personas, 0, 5)
    message = exc.value.message
    assert "banking" in message
    assert "healthcare, manufacturing, restaurant" in message
    assert not message.startswith(('"', "'"))  # not KeyError's repr-quoting


@pytest.mark.parametrize("ttl", ["0m", "2H", "1d", "90", "999h", ""])
def test_admission_bad_ttl_gives_duration_message(personas: dict[str, Persona], ttl: str) -> None:
    with pytest.raises(AdmissionError) as exc:
        check_admission({"persona": "healthcare", "ttl": ttl}, personas, 0, 5)
    assert "use h/m/s units" in exc.value.message


def test_admission_ttl_over_persona_max_rejected(personas: dict[str, Persona]) -> None:
    short = personas["healthcare"].model_copy(update={"max_ttl": timedelta(hours=1)})
    with pytest.raises(AdmissionError) as exc:
        check_admission({"persona": "healthcare", "ttl": "2h"}, {"healthcare": short}, 0, 5)
    assert "exceeds the maximum of 1h" in exc.value.message


def test_admission_capacity(personas: dict[str, Persona]) -> None:
    with pytest.raises(AdmissionError) as exc:
        check_admission({"persona": "healthcare", "ttl": "1h"}, personas, 5, 5)
    assert exc.value.message == "capacity: 5/5 environments in use"


# --- provision: first pass ---------------------------------------------------


def test_first_pass_persists_admission_before_any_cluster_work(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    with pytest.raises(kopf.TemporaryError) as exc:
        env.run()
    assert exc.value.delay == 1  # re-enter as soon as the status patch has landed

    assert env.status["phase"] == "Provisioning"
    assert env.status["createdAt"] == "2026-09-24T15:00:00.000Z"
    assert env.status["namespace"] == NAMESPACE
    assert env.status["expiresAt"] == "2026-09-24T15:10:00.000Z"
    assert _events(ledger) == [EventType.REQUESTED]
    assert kube.active_excluded == [NAME]
    assert kube.applied == []


def test_second_pass_applies_namespace_then_workloads(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    with pytest.raises(kopf.TemporaryError) as exc:
        env.run()
    assert exc.value.delay == 3
    assert _events(ledger) == [EventType.REQUESTED]

    kinds = kube.kinds_applied()
    assert kinds[0] == "Namespace"
    assert "Deployment" in kinds
    assert "Job" not in kinds  # seed waits for postgres
    namespace = kube.applied[0]
    assert namespace["metadata"]["ownerReferences"][0]["name"] == NAME
    assert namespace["metadata"]["ownerReferences"][0]["uid"] == UID
    assert namespace["metadata"]["labels"][LABEL_ENV] == NAME
    assert kube.namespace_annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == env.status["expiresAt"]


def test_second_on_create_with_created_at_writes_no_second_requested_event(
    monkeypatch: pytest.MonkeyPatch,
    deps: ProvisionDeps,
    kube: FakeKube,
    ledger: Ledger,
    clock: Clock,
) -> None:
    monkeypatch.setattr(handlers, "_deps", lambda: deps)
    status: dict[str, Any] = {}
    for _ in range(2):
        patch = kopf.Patch()
        with pytest.raises(kopf.TemporaryError):
            handlers.on_create(
                name=NAME,
                uid=UID,
                spec={"persona": "healthcare", "ttl": "10m"},
                status=copy.deepcopy(status),
                meta={"creationTimestamp": CREATION_TIMESTAMP},
                patch=patch,
            )
        _merge(status, dict(patch.get("status", {})))
        clock.advance(3)

    assert status["createdAt"] == "2026-09-24T15:00:00.000Z"
    assert _events(ledger) == [EventType.REQUESTED]


# --- provision: progression --------------------------------------------------


def test_seed_job_applied_only_after_postgres_available(
    deps: ProvisionDeps, kube: FakeKube, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    for _ in range(3):
        with pytest.raises(kopf.TemporaryError):
            env.run()
        clock.advance(3)
    assert "Job" not in kube.kinds_applied()
    assert env.status["phase"] == "Provisioning"

    kube.available.add((NAMESPACE, "postgres"))
    with pytest.raises(kopf.TemporaryError):
        env.run()
    assert env.status["phase"] == "Seeding"
    assert kube.kinds_applied().count("Job") == 1


def test_happy_path_reaches_ready_with_timings_and_ready_event(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()  # t=1: admitted, status persisted
    with pytest.raises(kopf.TemporaryError):
        env.run()  # t=1: namespace + workloads applied, postgres not ready

    clock.advance(10)  # t=11
    kube.available.add((NAMESPACE, "postgres"))
    with pytest.raises(kopf.TemporaryError):
        env.run()  # seeding starts, job running

    clock.advance(5)  # t=16
    kube.job = "succeeded"
    with pytest.raises(kopf.TemporaryError):
        env.run()  # seed done, crewline not yet available

    clock.advance(4)  # t=20
    kube.available.add((NAMESPACE, "crewline"))
    env.run()

    status = env.status
    assert status["phase"] == "Ready"
    assert status["url"] == f"http://{NAME}.demo.localtest.me"
    assert status["readyAt"] == "2026-09-24T15:00:20.000Z"
    assert status["timings"] == {
        "namespaceSeconds": 0.0,
        "appReadySeconds": 14.0,  # 10 s waiting for postgres + 4 s for crewline
        "seedSeconds": 5.0,
        "totalSeconds": 19.0,
    }
    assert _events(ledger) == [EventType.REQUESTED, EventType.READY]
    ready = list(ledger.read())[-1]
    assert ready.details == {"provisioning_seconds": 19.0, "timings": status["timings"]}
    assert ready.env == NAME
    assert ready.namespace == NAMESPACE
    assert ready.persona == "healthcare"

    # A further pass (e.g. after an operator restart) is a no-op.
    applied_before = len(kube.applied)
    env.run()
    assert len(kube.applied) == applied_before
    assert _events(ledger) == [EventType.REQUESTED, EventType.READY]


def test_timings_come_from_status_checkpoints_across_a_restart(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    with pytest.raises(kopf.TemporaryError):
        env.run()
    # Simulate a fresh operator process: only the persisted status survives.
    persisted = copy.deepcopy(env.status)
    fresh = Env(deps)
    fresh.status = persisted

    clock.advance(30)
    kube.available.update({(NAMESPACE, "postgres"), (NAMESPACE, "crewline")})
    kube.job = "succeeded"
    fresh.run()  # seed applied, succeeded and crewline available, all in one pass

    assert fresh.status["phase"] == "Ready"
    assert fresh.status["timings"]["totalSeconds"] == 30.0
    assert fresh.status["timings"]["appReadySeconds"] == 30.0
    assert _events(ledger) == [EventType.REQUESTED, EventType.READY]


# --- provision: failures -----------------------------------------------------


def test_over_capacity_fails_permanently(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    kube.active = 5
    env = Env(deps)
    with pytest.raises(kopf.PermanentError):
        env.run()
    assert env.status["phase"] == "Failed"
    assert env.status["message"] == "capacity: 5/5 environments in use"
    assert "namespace" not in env.status  # never created, so never reported
    assert from_rfc3339(env.status["expiresAt"]) == clock.now + timedelta(minutes=10)
    assert kube.applied == []
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.REQUESTED, EventType.FAILED]
    assert events[-1].details == {"reason": "capacity: 5/5 environments in use"}


def test_unknown_persona_fails_permanently_with_readable_message(
    deps: ProvisionDeps, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps, spec={"persona": "banking", "ttl": "1h"})
    with pytest.raises(kopf.PermanentError):
        env.run()
    assert env.status["phase"] == "Failed"
    assert env.status["message"].startswith("unknown persona 'banking'")
    assert _events(ledger) == [EventType.REQUESTED, EventType.FAILED]


def test_seed_job_failure_fails_and_extends_namespace_expiry(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    kube.available.add((NAMESPACE, "postgres"))
    env.admit()
    with pytest.raises(kopf.TemporaryError):
        env.run()
    clock.advance(20)
    kube.job = "failed"
    with pytest.raises(kopf.PermanentError):
        env.run()
    expected = to_rfc3339(clock.now + timedelta(minutes=10))
    assert env.status["phase"] == "Failed"
    assert "seed" in env.status["message"]
    assert env.status["expiresAt"] == expected
    assert kube.namespace_annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == expected
    assert _events(ledger) == [EventType.REQUESTED, EventType.FAILED]


def test_provision_timeout_measured_from_created_at(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    with pytest.raises(kopf.TemporaryError):
        env.run()
    clock.now = CREATED + timedelta(seconds=299)
    with pytest.raises(kopf.TemporaryError):
        env.run()
    clock.now = CREATED + timedelta(seconds=301)
    with pytest.raises(kopf.PermanentError):
        env.run()
    expected = to_rfc3339(clock.now + timedelta(minutes=10))
    assert env.status["phase"] == "Failed"
    assert "timeout" in env.status["message"]
    assert env.status["expiresAt"] == expected
    assert kube.namespace_annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == expected
    assert _events(ledger) == [EventType.REQUESTED, EventType.FAILED]


def test_failed_status_is_not_reprovisioned(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    kube.active = 5
    env = Env(deps)
    with pytest.raises(kopf.PermanentError):
        env.run()
    kube.active = 0
    env.run()
    assert env.status["phase"] == "Failed"
    assert kube.applied == []
    assert _events(ledger) == [EventType.REQUESTED, EventType.FAILED]


def test_crd_status_schema_keeps_checkpoints() -> None:
    """The structural schema prunes unknown status fields, so checkpoints must be declared."""
    crd = yaml.safe_load((REPO_ROOT / "deploy" / "crd" / "demoenvironments.yaml").read_text())
    status = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]
    declared = set(status["properties"]["checkpoints"]["properties"])
    assert declared == {
        provision.CP_PROVISIONING,
        provision.CP_WORKLOADS,
        provision.CP_SEEDING,
        provision.CP_SEEDED,
    }


def test_name_too_long_for_a_namespace_fails_permanently(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    patch: dict[str, Any] = {}
    with pytest.raises(kopf.PermanentError):
        provision.provision(
            name="healthcare-" + "x" * 60,
            uid=UID,
            spec={"persona": "healthcare", "ttl": "10m"},
            status={},
            creation_timestamp=CREATION_TIMESTAMP,
            patch_status=patch,
            deps=deps,
        )
    assert patch["phase"] == "Failed"
    assert "63-character" in patch["message"]
    assert kube.applied == []
    assert _events(ledger) == [EventType.REQUESTED, EventType.FAILED]


# --- provision: TTL changes while provisioning --------------------------------


def test_ttl_change_mid_provision_resyncs_namespace_annotation(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    with pytest.raises(kopf.TemporaryError):
        env.run()  # namespace applied with the 10m expiry
    env.spec = {**env.spec, "ttl": "20m"}
    applied_before = len(kube.applied)
    with pytest.raises(kopf.TemporaryError):
        env.run()
    assert env.status["expiresAt"] == "2026-09-24T15:20:00.000Z"
    assert kube.namespace_annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == env.status["expiresAt"]
    assert len(kube.applied) == applied_before  # a merge patch, not a re-apply


def test_invalid_ttl_mid_provision_keeps_expiry_and_sets_message(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    with pytest.raises(kopf.TemporaryError):
        env.run()
    env.spec = {**env.spec, "ttl": "9h"}
    kube.available.update({(NAMESPACE, "postgres"), (NAMESPACE, "crewline")})
    kube.job = "succeeded"
    env.run()
    assert env.status["phase"] == "Ready"
    assert env.status["expiresAt"] == "2026-09-24T15:10:00.000Z"
    assert "use h/m/s units" in env.status["message"]
    assert kube.namespace_annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == env.status["expiresAt"]
    assert _events(ledger) == [EventType.REQUESTED, EventType.READY]


# --- handlers: transient errors and log noise ---------------------------------


class _FlakyKube(FakeKube):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def apply(self, manifest: dict[str, Any]) -> None:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        ApiException(status=500, reason="Internal Server Error"),
        ApiException(status=503, reason="Service Unavailable"),
        ApiException(status=409, reason="Conflict"),
        ApiException(status=429, reason="Too Many Requests"),
        urllib3.exceptions.MaxRetryError(pool=None, url="/api"),  # type: ignore[arg-type]
        urllib3.exceptions.ProtocolError("Connection aborted."),
    ],
)
def test_transient_api_errors_retry_in_3s(
    monkeypatch: pytest.MonkeyPatch,
    deps: ProvisionDeps,
    clock: Clock,
    error: Exception,
) -> None:
    flaky = dataclasses.replace(deps, kube=_FlakyKube(error))
    monkeypatch.setattr(handlers, "_deps", lambda: flaky)
    status = {
        "phase": "Provisioning",
        "createdAt": "2026-09-24T15:00:00.000Z",
        "namespace": NAMESPACE,
    }
    with pytest.raises(kopf.TemporaryError) as exc:
        handlers.on_create(
            name=NAME,
            uid=UID,
            spec={"persona": "healthcare", "ttl": "10m"},
            status=status,
            meta={"creationTimestamp": CREATION_TIMESTAMP},
            patch=kopf.Patch(),
        )
    assert exc.value.delay == 3
    assert str(exc.value).startswith("transient API error")


def test_non_transient_api_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, deps: ProvisionDeps, clock: Clock
) -> None:
    flaky = dataclasses.replace(deps, kube=_FlakyKube(ApiException(status=403, reason="Forbidden")))
    monkeypatch.setattr(handlers, "_deps", lambda: flaky)
    with pytest.raises(ApiException):
        handlers.on_create(
            name=NAME,
            uid=UID,
            spec={"persona": "healthcare", "ttl": "10m"},
            status={"createdAt": "2026-09-24T15:00:00.000Z", "namespace": NAMESPACE},
            meta={"creationTimestamp": CREATION_TIMESTAMP},
            patch=kopf.Patch(),
        )


def _record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("kopf.objects", level, __file__, 1, message, None, None)


def test_routine_waits_are_not_posted_or_logged_as_errors() -> None:
    quiet = handlers.QuietWaitsFilter()
    record = _record("Handler 'on_create' failed temporarily: waiting for the seed Job to succeed")
    quiet.filter(record)
    assert record.levelno == logging.DEBUG
    assert record.levelname == "DEBUG"
    assert getattr(record, "k8s_skip", False) is True


def test_transient_api_errors_are_logged_as_warnings() -> None:
    record = _record("Handler 'on_create' failed temporarily: transient API error: 503")
    assert handlers.QuietWaitsFilter().filter(record) is True
    assert record.levelno == logging.WARNING
    assert not getattr(record, "k8s_skip", False)


def test_permanent_failures_stay_errors_and_are_posted() -> None:
    record = _record("Handler 'on_create' failed permanently: seed job failed")
    assert handlers.QuietWaitsFilter().filter(record) is True
    assert record.levelno == logging.ERROR
    assert not getattr(record, "k8s_skip", False)


def test_expiring_env_is_not_provisioned_further(
    deps: ProvisionDeps, kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    env = Env(deps)
    env.admit()
    env.status["phase"] = "Expiring"  # the expiry timer got there first
    env.run()
    assert env.status["phase"] == "Expiring"
    assert kube.applied == []
