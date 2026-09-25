"""Unit tests for the sweeper's executor (`orchestrator.sweeper.main`).

Driven against an in-memory fake of the Kubernetes API, with a fake clock whose
`sleep` just advances time, so the 30 s CR wait costs nothing.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kubernetes.client.exceptions import ApiException

from orchestrator.config import Settings
from orchestrator.constants import (
    ANNOTATION_EXPIRES_AT,
    FINALIZER,
    LABEL_ENV,
    LABEL_MANAGED_BY,
    LABEL_PERSONA,
    MANAGED_BY_VALUE,
)
from orchestrator.core.expiry import to_rfc3339
from orchestrator.core.ledger import EventType, Ledger, LedgerEvent
from orchestrator.core.sweep import NsInfo
from orchestrator.k8s.client import NamespaceInfo
from orchestrator.sweeper import main
from orchestrator.sweeper.main import Sweeper

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
GRACE = timedelta(seconds=120)
EXPIRED = NOW - timedelta(minutes=10)


def managed_labels(env: str | None, persona: str = "healthcare") -> dict[str, str]:
    labels = {LABEL_MANAGED_BY: MANAGED_BY_VALUE, LABEL_PERSONA: persona}
    if env is not None:
        labels[LABEL_ENV] = env
    return labels


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += seconds


class FakeKube:
    """Namespaces and DemoEnvironments in memory. `calls` records every write, in order."""

    def __init__(self, *, operator_up: bool = False) -> None:
        self.operator_up = operator_up
        self.namespaces: dict[str, NsInfo] = {}
        self.envs: dict[str, list[str]] = {}
        self.deleting_envs: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        # Label overrides applied to the *fresh* fetch only, to simulate a change
        # between the listing (plan) and the pre-delete read.
        self.fresh_labels: dict[str, dict[str, str]] = {}
        self.conflicts: dict[str, int] = {}
        self.fail_env_delete = False
        self.fail_listing = False

    def add_ns(
        self,
        name: str,
        env: str | None,
        *,
        expires: datetime | None = EXPIRED,
        age: timedelta = timedelta(hours=1),
        labels: dict[str, str] | None = None,
        phase: str = "Active",
        deletion_started_at: datetime | None = None,
    ) -> None:
        annotations = {ANNOTATION_EXPIRES_AT: to_rfc3339(expires)} if expires else {}
        self.namespaces[name] = NsInfo(
            name,
            labels if labels is not None else managed_labels(env),
            annotations,
            NOW - age,
            phase,
            deletion_started_at,
        )

    def add_env(self, name: str, finalizers: list[str] | None = None) -> None:
        self.envs[name] = [FINALIZER] if finalizers is None else finalizers

    # --- SweeperKube ---

    def list_managed_namespaces(self) -> list[NsInfo]:
        if self.fail_listing:
            raise ApiException(status=503, reason="Service Unavailable")
        return list(self.namespaces.values())

    def list_env_names(self) -> set[str]:
        if self.fail_listing:
            raise ApiException(status=503, reason="Service Unavailable")
        return set(self.envs)

    def get_env_finalizers(self, name: str) -> list[str] | None:
        return list(self.envs[name]) if name in self.envs else None

    def delete_env(self, name: str) -> None:
        if self.fail_env_delete:
            raise ApiException(status=500, reason="boom")
        self.calls.append(("delete_env", name))
        if name not in self.envs:
            return
        self.deleting_envs.add(name)
        if self.operator_up:
            # The operator's finalizer tears the namespace down and lets go.
            self.namespaces.pop(f"demo-{name}", None)
            self.envs[name] = [f for f in self.envs[name] if f != FINALIZER]
        if not self.envs[name]:
            del self.envs[name]

    def remove_env_finalizer(self, name: str, finalizer: str) -> bool:
        self.calls.append(("remove_env_finalizer", name))
        if name not in self.envs or finalizer not in self.envs[name]:
            return False
        self.envs[name].remove(finalizer)
        if not self.envs[name] and name in self.deleting_envs:
            del self.envs[name]
        return True

    def get_namespace(self, name: str) -> NamespaceInfo | None:
        ns = self.namespaces.get(name)
        if ns is None:
            return None
        return NamespaceInfo(
            name=ns.name,
            uid=f"uid-{name}",
            resource_version="1",
            labels=self.fresh_labels.get(name, dict(ns.labels)),
            terminating=ns.phase == "Terminating",
        )

    def delete_namespace(self, ns: NamespaceInfo) -> None:
        self.calls.append(("delete_namespace", ns.name))
        if self.conflicts.get(ns.name, 0) > 0:
            self.conflicts[ns.name] -= 1
            # Someone else (the GC) started deleting it in the meantime.
            self.namespaces[ns.name] = replace(self.namespaces[ns.name], phase="Terminating")
            raise ApiException(status=409, reason="Conflict")
        self.namespaces[ns.name] = replace(self.namespaces[ns.name], phase="Terminating")


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "utcnow", lambda: NOW)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.jsonl")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def sweeper(kube: FakeKube, ledger: Ledger, clock: FakeClock, **kwargs: float) -> Sweeper:
    return Sweeper(kube=kube, ledger=ledger, grace=GRACE, sleep=clock.sleep, clock=clock, **kwargs)


def events(ledger: Ledger, kind: EventType | None = None) -> list[LedgerEvent]:
    return [e for e in ledger.read() if kind is None or e.event == kind]


def test_guard_recheck_on_fresh_object_refuses_delete(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")
    # Planned from a managed listing, but the label is gone by the time we re-read it.
    kube.fresh_labels["demo-healthcare-ab12"] = {LABEL_ENV: "healthcare-ab12"}

    report = sweeper(kube, ledger, clock).run()

    assert ("delete_namespace", "demo-healthcare-ab12") not in kube.calls
    assert report.refused == ["demo-healthcare-ab12"]
    assert report.reaped == []
    assert events(ledger, EventType.SWEEPER_REAPED) == []


def test_unmanaged_and_protected_namespaces_are_never_touched(
    ledger: Ledger, clock: FakeClock
) -> None:
    kube = FakeKube()
    kube.add_ns("demo-imposter", None, labels={}, age=timedelta(hours=9))
    kube.add_ns("kube-system", "x", labels=managed_labels("x"))
    kube.add_ns("demo-orchestrator", "x", labels=managed_labels("x"))

    report = sweeper(kube, ledger, clock).run()

    assert kube.calls == []
    assert report.reaped == report.refused == report.failed == []
    assert events(ledger) == []


def test_existing_cr_is_deleted_before_the_namespace(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube(operator_up=True)
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")
    kube.add_env("healthcare-ab12")

    report = sweeper(kube, ledger, clock).run()

    assert kube.calls[0] == ("delete_env", "healthcare-ab12")
    # The operator's finalizer deleted the namespace; no finalizer surgery needed.
    assert ("remove_env_finalizer", "healthcare-ab12") not in kube.calls
    assert ("delete_namespace", "demo-healthcare-ab12") not in kube.calls
    assert clock.slept == 0
    assert report.reaped == ["demo-healthcare-ab12"]
    (event,) = events(ledger, EventType.SWEEPER_REAPED)
    assert event.details["cr_existed"] is True
    assert event.details["finalizer_removed"] is False
    assert event.details["namespace_outcome"] == "already_gone"


def test_stuck_finalizer_is_patched_out_after_the_wait(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube(operator_up=False)
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")
    kube.add_env("healthcare-ab12", finalizers=[FINALIZER, "other.example/keep"])

    sweeper(kube, ledger, clock).run()

    assert kube.calls == [
        ("delete_env", "healthcare-ab12"),
        ("remove_env_finalizer", "healthcare-ab12"),
        ("delete_namespace", "demo-healthcare-ab12"),
    ]
    assert clock.slept >= main.CR_WAIT_SECONDS
    # Only our finalizer is removed; others are left for their owners.
    assert kube.envs["healthcare-ab12"] == ["other.example/keep"]
    (event,) = events(ledger, EventType.SWEEPER_REAPED)
    assert event.details["cr_existed"] is True
    assert event.details["finalizer_removed"] is True
    assert event.details["namespace_outcome"] == "deleted"


def test_one_shared_wait_for_many_stuck_crs(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube(operator_up=False)
    for suffix in ("aaaa", "bbbb", "cccc"):
        kube.add_ns(f"demo-healthcare-{suffix}", f"healthcare-{suffix}")
        kube.add_env(f"healthcare-{suffix}")

    report = sweeper(kube, ledger, clock).run()

    assert main.CR_WAIT_SECONDS <= clock.slept < main.CR_WAIT_SECONDS + main.POLL_INTERVAL_SECONDS
    assert len(report.reaped) == 3
    assert kube.envs == {}


def test_reaped_event_contents(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12", expires=EXPIRED)

    sweeper(kube, ledger, clock).run()

    (event,) = events(ledger)
    assert event.event == EventType.SWEEPER_REAPED
    assert event.actor == "sweeper"
    assert event.env == "healthcare-ab12"
    assert event.namespace == "demo-healthcare-ab12"
    assert event.persona == "healthcare"
    assert event.ts == NOW
    assert event.details == {
        "kind": "reap_expired",
        "reason": "expired at 2026-09-24T11:50:00.000Z, past the 120s grace",
        "lag_seconds": 600.0,
        "cr_existed": False,
        "finalizer_removed": False,
        "namespace_outcome": "deleted",
    }


def test_orphan_without_annotation_or_env_label(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns("demo-stray", None, expires=None, age=timedelta(minutes=6))

    report = sweeper(kube, ledger, clock).run()

    assert kube.calls == [("delete_namespace", "demo-stray")]
    assert report.reaped == ["demo-stray"]
    (event,) = events(ledger)
    assert event.env == ""
    assert event.details["kind"] == "reap_orphan"
    assert event.details["lag_seconds"] is None


def test_cr_of_a_different_namespace_is_not_deleted(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    # This namespace's env label names a real env, but not the one it belongs to.
    kube.add_ns("demo-healthcare-zzzz", "healthcare-ab12")
    kube.add_env("healthcare-ab12")

    sweeper(kube, ledger, clock).run()

    assert kube.calls == [("delete_namespace", "demo-healthcare-zzzz")]
    assert "healthcare-ab12" in kube.envs


def test_failed_cr_delete_still_reaps_the_namespace(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")
    kube.add_env("healthcare-ab12")
    kube.fail_env_delete = True

    report = sweeper(kube, ledger, clock).run()

    assert kube.calls == [("delete_namespace", "demo-healthcare-ab12")]
    assert report.reaped == ["demo-healthcare-ab12"]


def test_conflict_rereads_and_rechecks(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")
    kube.conflicts["demo-healthcare-ab12"] = 1

    report = sweeper(kube, ledger, clock).run()

    assert report.reaped == ["demo-healthcare-ab12"]
    (event,) = events(ledger)
    assert event.details["namespace_outcome"] == "already_terminating"


def test_run_budget_defers_leftovers(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")
    clock.t = 0.0

    report = sweeper(kube, ledger, clock, budget=-1.0).run()

    assert report.deferred == ["demo-healthcare-ab12"]
    assert kube.calls == []
    assert events(ledger) == []


def test_terminating_is_logged_once_per_deletion(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.add_ns(
        "demo-healthcare-ab12",
        "healthcare-ab12",
        phase="Terminating",
        deletion_started_at=NOW - timedelta(seconds=5),
    )

    first = sweeper(kube, ledger, clock).run()
    second = sweeper(kube, ledger, clock).run()

    assert first.skipped == second.skipped == ["demo-healthcare-ab12"]
    assert kube.calls == []
    (event,) = events(ledger)
    assert event.event == EventType.SWEEPER_SKIPPED
    assert event.actor == "sweeper"
    assert event.persona == "healthcare"
    assert event.details == {
        "kind": "skip_terminating",
        "reason": "namespace is already terminating",
        "deletion_started_at": "2026-09-24T11:59:55.000Z",
    }


def test_listing_failure_raises(ledger: Ledger, clock: FakeClock) -> None:
    kube = FakeKube()
    kube.fail_listing = True
    with pytest.raises(ApiException):
        sweeper(kube, ledger, clock).run()


def _patch_main(monkeypatch: pytest.MonkeyPatch, kube: FakeKube, tmp_path: Path) -> None:
    monkeypatch.setattr(main, "load_kube_config", lambda: None)
    monkeypatch.setattr(main, "KubeClient", lambda: kube)
    monkeypatch.setattr(
        main, "get_settings", lambda: Settings(ledger_path=tmp_path / "ledger.jsonl")
    )


def test_main_exits_nonzero_when_cluster_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kube = FakeKube()
    kube.fail_listing = True
    _patch_main(monkeypatch, kube, tmp_path)
    assert main.main() == 1


def test_main_exits_zero_when_an_action_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kube = FakeKube()
    kube.add_ns("demo-healthcare-ab12", "healthcare-ab12")

    def broken_delete(ns: NamespaceInfo) -> None:
        raise ApiException(status=500, reason="boom")

    monkeypatch.setattr(kube, "delete_namespace", broken_delete)
    _patch_main(monkeypatch, kube, tmp_path)
    assert main.main() == 0


def test_sweeper_does_not_import_kopf() -> None:
    code = "import sys, orchestrator.sweeper.main; sys.exit('kopf' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
