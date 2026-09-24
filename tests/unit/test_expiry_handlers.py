"""Unit tests for TTL expiry, extension and finalizer-backed teardown.

Driven through the kopf handler functions with a fake Kubernetes facade and a
frozen clock (`lifecycle.utcnow`).
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import kopf
import pytest

from orchestrator.constants import (
    ANNOTATION_EXPIRES_AT,
    LABEL_ENV,
    LABEL_MANAGED_BY,
    MANAGED_BY_VALUE,
)
from orchestrator.core.expiry import to_rfc3339
from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.personas import Persona, load_personas
from orchestrator.k8s.client import NamespaceInfo
from orchestrator.operator import handlers, lifecycle
from orchestrator.operator.lifecycle import LifecycleDeps

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = REPO_ROOT / "personas"

NAME = "healthcare-ab12"
NAMESPACE = "demo-healthcare-ab12"
CREATED = datetime(2026, 9, 24, 15, 0, 0, tzinfo=UTC)
CREATED_AT = "2026-09-24T15:00:00.000Z"
EXPIRES = CREATED + timedelta(minutes=10)
EXPIRES_AT = "2026-09-24T15:10:00.000Z"


class FakeKube:
    """In-memory stand-in for the lifecycle's slice of `KubeFacade`."""

    def __init__(self) -> None:
        self.namespaces: dict[str, NamespaceInfo] = {}
        self.annotations: dict[str, dict[str, str]] = {}
        self.namespace_deletes: list[str] = []
        self.env_status_patches: list[tuple[str, dict[str, Any]]] = []
        self.env_deletes: list[str] = []

    def add_namespace(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.namespaces[name] = NamespaceInfo(
            name=name,
            uid=f"uid-{name}",
            resource_version="1",
            labels=labels if labels is not None else {LABEL_MANAGED_BY: MANAGED_BY_VALUE},
            terminating=False,
        )
        self.annotations[name] = {ANNOTATION_EXPIRES_AT: EXPIRES_AT}

    def get_namespace(self, name: str) -> NamespaceInfo | None:
        return self.namespaces.get(name)

    def delete_namespace(self, ns: NamespaceInfo) -> None:
        self.namespace_deletes.append(ns.name)
        self.namespaces[ns.name] = replace(ns, terminating=True)

    def finish_namespace_deletion(self, name: str) -> None:
        self.namespaces.pop(name, None)

    def set_namespace_expiry(self, ns: str, expires_at: str) -> None:
        if ns in self.annotations:
            self.annotations[ns][ANNOTATION_EXPIRES_AT] = expires_at

    def patch_env_status(self, name: str, status: dict[str, Any]) -> None:
        self.env_status_patches.append((name, copy.deepcopy(status)))

    def delete_env(self, name: str) -> None:
        self.env_deletes.append(name)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


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
    clock = Clock(CREATED + timedelta(minutes=5))
    monkeypatch.setattr(lifecycle, "utcnow", clock)
    return clock


@pytest.fixture(autouse=True)
def deps(
    monkeypatch: pytest.MonkeyPatch, kube: FakeKube, ledger: Ledger, personas: dict[str, Persona]
) -> LifecycleDeps:
    deps = LifecycleDeps(kube=kube, ledger=ledger, personas=personas)
    monkeypatch.setattr(handlers, "_lifecycle_deps", lambda: deps)
    return deps


def _status(**overrides: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "phase": "Ready",
        "createdAt": CREATED_AT,
        "expiresAt": EXPIRES_AT,
        "namespace": NAMESPACE,
    }
    status.update(overrides)
    return {k: v for k, v in status.items() if v is not None}


SPEC = {"persona": "healthcare", "ttl": "10m"}


def _events(ledger: Ledger) -> list[EventType]:
    return [e.event for e in ledger.read()]


# --- timer --------------------------------------------------------------------


def _tick(status: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
    handlers.expiry_timer(name=NAME, spec=SPEC, status=status, meta=meta or {})


def test_timer_does_nothing_before_expires_at(kube: FakeKube, ledger: Ledger, clock: Clock) -> None:
    clock.now = EXPIRES - timedelta(milliseconds=1)
    _tick(_status())
    assert kube.env_status_patches == []
    assert kube.env_deletes == []
    assert _events(ledger) == []


@pytest.mark.parametrize("after", [timedelta(0), timedelta(seconds=7)])
def test_timer_expires_at_or_after_expires_at(
    kube: FakeKube, ledger: Ledger, clock: Clock, after: timedelta
) -> None:
    clock.now = EXPIRES + after
    _tick(_status())
    assert kube.env_status_patches == [(NAME, {"phase": "Expiring", "message": "TTL expired"})]
    assert kube.env_deletes == [NAME]
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.EXPIRED]
    assert events[0].details == {"expires_at": EXPIRES_AT, "previous_phase": "Ready"}
    assert events[0].namespace == NAMESPACE
    assert events[0].persona == "healthcare"


def test_timer_ignores_env_without_expires_at(kube: FakeKube, clock: Clock) -> None:
    clock.now = CREATED + timedelta(hours=9)
    _tick(_status(expiresAt=None))
    assert kube.env_deletes == []


def test_timer_retries_delete_without_second_expired_event(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    """A crash between marking Expiring and deleting must still delete, once-logged."""
    clock.now = EXPIRES + timedelta(seconds=30)
    _tick(_status(phase="Expiring"))
    assert kube.env_status_patches == []
    assert kube.env_deletes == [NAME]
    assert _events(ledger) == []


def test_timer_skips_env_already_being_deleted(kube: FakeKube, clock: Clock) -> None:
    clock.now = EXPIRES + timedelta(seconds=30)
    _tick(_status(phase="Expiring"), meta={"deletionTimestamp": "2026-09-24T15:10:01Z"})
    assert kube.env_deletes == []


def test_failed_env_expires_through_the_same_path(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    failed_expiry = CREATED + timedelta(minutes=11)
    clock.now = failed_expiry
    _tick(_status(phase="Failed", expiresAt=to_rfc3339(failed_expiry), namespace=None))
    assert kube.env_deletes == [NAME]
    assert _events(ledger) == [EventType.EXPIRED]


# --- extend -------------------------------------------------------------------


def _extend(new_ttl: str, status: dict[str, Any], old_ttl: str | None = "10m") -> dict[str, Any]:
    patch = kopf.Patch()
    handlers.on_ttl_change(
        name=NAME,
        old=old_ttl,
        new=new_ttl,
        spec={**SPEC, "ttl": new_ttl},
        status=status,
        patch=patch,
    )
    return dict(patch.get("status", {}))


def test_extend_within_max_updates_status_annotation_and_ledger(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    kube.add_namespace(NAMESPACE)
    patch = _extend("1h", _status())
    new_expires_at = "2026-09-24T16:00:00.000Z"
    assert patch["expiresAt"] == new_expires_at
    assert patch["message"] == ""
    assert kube.annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == new_expires_at
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.EXTENDED]
    assert events[0].details == {
        "old_expires_at": EXPIRES_AT,
        "new_expires_at": new_expires_at,
    }


def test_extend_beyond_persona_max_keeps_expires_at_and_sets_message(
    monkeypatch: pytest.MonkeyPatch,
    kube: FakeKube,
    ledger: Ledger,
    clock: Clock,
    personas: dict[str, Persona],
) -> None:
    short = personas["healthcare"].model_copy(update={"max_ttl": timedelta(hours=1)})
    deps = LifecycleDeps(kube=kube, ledger=ledger, personas={"healthcare": short})
    monkeypatch.setattr(handlers, "_lifecycle_deps", lambda: deps)
    kube.add_namespace(NAMESPACE)
    patch = _extend("2h", _status())
    assert "expiresAt" not in patch
    assert "exceeds the maximum of 1h" in patch["message"]
    assert kube.annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == EXPIRES_AT
    assert _events(ledger) == []


@pytest.mark.parametrize("ttl", ["2H", "9h"])
def test_extend_with_bad_ttl_sets_readable_message(
    kube: FakeKube, ledger: Ledger, clock: Clock, ttl: str
) -> None:
    kube.add_namespace(NAMESPACE)
    patch = _extend(ttl, _status())
    assert "expiresAt" not in patch
    assert "use h/m/s units" in patch["message"]
    assert kube.annotations[NAMESPACE][ANNOTATION_EXPIRES_AT] == EXPIRES_AT
    assert _events(ledger) == []


def test_extend_ignores_initial_creation(kube: FakeKube, ledger: Ledger, clock: Clock) -> None:
    assert _extend("1h", _status(), old_ttl=None) == {}
    assert _events(ledger) == []


def test_extend_ignores_env_still_being_admitted(ledger: Ledger, clock: Clock) -> None:
    assert _extend("1h", _status(createdAt=None, expiresAt=None, phase=None)) == {}
    assert _events(ledger) == []


@pytest.mark.parametrize("phase", ["Failed", "Expiring"])
def test_extend_ignores_failed_and_expiring_envs(ledger: Ledger, clock: Clock, phase: str) -> None:
    assert _extend("1h", _status(phase=phase)) == {}
    assert _events(ledger) == []


# --- delete (finalizer) -------------------------------------------------------


def _finalize(status: dict[str, Any], deletion_ts: str = "2026-09-24T15:05:00Z") -> None:
    handlers.on_delete(name=NAME, spec=SPEC, status=status, meta={"deletionTimestamp": deletion_ts})


def test_manual_delete_before_expiry_deletes_namespace_then_records_manual(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    kube.add_namespace(NAMESPACE)
    with pytest.raises(kopf.TemporaryError) as exc:
        _finalize(_status())
    assert exc.value.delay == 3
    assert kube.namespace_deletes == [NAMESPACE]
    assert _events(ledger) == []

    clock.now += timedelta(seconds=3)
    with pytest.raises(kopf.TemporaryError):
        _finalize(_status())
    assert kube.namespace_deletes == [NAMESPACE]  # terminating: not re-issued

    kube.finish_namespace_deletion(NAMESPACE)
    clock.now += timedelta(seconds=3)
    _finalize(_status())
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.DELETED]
    # Deleted 6 s after 15:05:00, expiry was 15:10:00 → negative lag, recorded truthfully.
    assert events[0].details == {"lag_seconds": -294.0, "reason": "manual"}


def test_delete_after_timer_expiry_records_reason_expired(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    clock.now = EXPIRES + timedelta(seconds=12.5)
    _finalize(_status(phase="Expiring"), deletion_ts="2026-09-24T15:10:05Z")
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.DELETED]
    assert events[0].details == {"lag_seconds": 12.5, "reason": "expired"}


def test_delete_of_failed_env_without_namespace_is_immediate(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    clock.now = EXPIRES + timedelta(seconds=1)
    _finalize(_status(phase="Expiring", namespace=None))
    assert kube.namespace_deletes == []
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.DELETED]
    assert events[0].details["reason"] == "expired"


def test_delete_without_expires_at_records_null_lag(ledger: Ledger, clock: Clock) -> None:
    _finalize({})
    events = list(ledger.read())
    assert events[0].details == {"lag_seconds": None, "reason": "manual"}


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {LABEL_ENV: NAME},
        {LABEL_MANAGED_BY: "someone-else"},
    ],
)
def test_delete_never_deletes_a_namespace_the_guard_rejects(
    kube: FakeKube, ledger: Ledger, clock: Clock, labels: dict[str, str]
) -> None:
    kube.add_namespace(NAMESPACE, labels=labels)
    _finalize(_status())
    assert kube.namespace_deletes == []
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.DELETE_TIMEOUT]
    assert events[0].details["reason"] == "manual"
    assert "deletion guard refused" in events[0].details["error"]


def test_delete_times_out_after_120s_and_releases_finalizer(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    kube.add_namespace(NAMESPACE)
    clock.now = datetime(2026, 9, 24, 15, 5, 0, tzinfo=UTC) + timedelta(seconds=119)
    with pytest.raises(kopf.TemporaryError):
        _finalize(_status())
    clock.now += timedelta(seconds=2)  # 121 s after deletionTimestamp
    _finalize(_status())  # returns: the finalizer is released
    events = list(ledger.read())
    assert [e.event for e in events] == [EventType.DELETE_TIMEOUT]
    assert events[0].details["elapsed_seconds"] == 121.0
    assert events[0].details["reason"] == "manual"


def test_name_too_long_for_a_namespace_is_deleted_immediately(
    kube: FakeKube, ledger: Ledger, clock: Clock
) -> None:
    handlers.on_delete(
        name="healthcare-" + "x" * 60,
        spec=SPEC,
        status={"phase": "Failed"},
        meta={"deletionTimestamp": "2026-09-24T15:05:00Z"},
    )
    assert _events(ledger) == [EventType.DELETED]
