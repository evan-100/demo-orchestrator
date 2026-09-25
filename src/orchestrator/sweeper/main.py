"""The sweeper: reap expired and orphaned demo namespaces, even while the operator is down.

Run once per minute by the CronJob (`python -m orchestrator.sweeper.main`). It
lists DemoEnvironments and managed namespaces, asks `core.sweep.plan_sweep` what
to do, and executes the plan in three phases so a run stays short however many
namespaces need reaping:

1. For each namespace to reap whose DemoEnvironment still exists, delete the CR.
   If the operator is up, its finalizer deletes the namespace and logs `deleted`.
2. Wait (once, for all of them) up to `cr_wait` seconds for those CRs to go. A
   CR still holding the operator's finalizer after that means the operator is
   down or stuck, so the finalizer is patched out.
3. Delete each namespace through the guarded path: re-fetch it, re-check
   `is_deletable_namespace` against the fresh object, then delete with
   uid/resourceVersion preconditions (`KubeClient.delete_namespace`, which
   checks the guard once more). A namespace failing the guard is never deleted,
   whatever the plan said.

Each reap is logged as `sweeper_reaped`. A namespace that is already
terminating is logged as `sweeper_skipped` once per deletion, not once per run
(see `_log_skips`). Individual failures are logged and skipped (the next run
retries them) and the exit code stays 0. It is non-zero only when the cluster
can't be listed at all: planning from a partial view (say, namespaces without
the CR list) would make every namespace look orphaned.

This module must never import kopf (spec A4): it has to work when the operator,
and anything kopf-shaped, is broken.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Protocol

from kubernetes.client.exceptions import ApiException

from orchestrator.config import get_settings
from orchestrator.constants import FINALIZER, LABEL_PERSONA
from orchestrator.core.expiry import to_rfc3339, utcnow
from orchestrator.core.guard import is_deletable_namespace
from orchestrator.core.ledger import EventType, Ledger, LedgerEvent
from orchestrator.core.naming import namespace_for
from orchestrator.core.sweep import NsInfo, SweepAction, parse_expires_at, plan_sweep
from orchestrator.k8s.client import (
    KubeClient,
    NamespaceDeletionRefusedError,
    NamespaceInfo,
    load_kube_config,
)

logger = logging.getLogger("orchestrator.sweeper")

CR_WAIT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 2.0
# The run stops starting new namespace deletes after this long, so the next
# minute's run (concurrencyPolicy: Forbid) isn't skipped; leftovers go to it.
RUN_BUDGET_SECONDS = 50.0
DELETE_ATTEMPTS = 2

NamespaceOutcome = Literal["deleted", "already_gone", "already_terminating", "refused"]


class SweeperKube(Protocol):
    """The slice of the Kubernetes API the sweeper needs (implemented by `KubeClient`)."""

    def list_managed_namespaces(self) -> list[NsInfo]: ...

    def list_env_names(self) -> set[str]: ...

    def get_env_finalizers(self, name: str) -> list[str] | None: ...

    def delete_env(self, name: str) -> None: ...

    def remove_env_finalizer(self, name: str, finalizer: str) -> bool: ...

    def get_namespace(self, name: str) -> NamespaceInfo | None: ...

    def delete_namespace(self, ns: NamespaceInfo) -> None: ...


@dataclass
class _Reap:
    """One reap action's progress through the three phases."""

    action: SweepAction
    ns: NsInfo
    cr: str | None = None  # the DemoEnvironment deleted in phase 1, if any
    cr_existed: bool = False
    finalizer_removed: bool = False


@dataclass
class SweepReport:
    reaped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)


def _owning_env(action: SweepAction) -> str | None:
    """The CR to delete for this namespace: its env label, if it names exactly this namespace.

    A namespace whose env label points at some other env's namespace must not
    get that other env's CR deleted.
    """
    if action.env is None:
        return None
    try:
        return action.env if namespace_for(action.env) == action.namespace else None
    except ValueError:
        return None


@dataclass
class Sweeper:
    kube: SweeperKube
    ledger: Ledger
    grace: timedelta
    cr_wait: float = CR_WAIT_SECONDS
    poll_interval: float = POLL_INTERVAL_SECONDS
    budget: float = RUN_BUDGET_SECONDS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def run(self) -> SweepReport:
        """One sweep. Raises only if the cluster can't be listed; per-action errors are logged."""
        started = self.clock()
        # CRs first: a namespace created after this listing is younger than the
        # orphan age floor, so it can't be mistaken for an orphan.
        envs = self.kube.list_env_names()
        namespaces = self.kube.list_managed_namespaces()
        actions = plan_sweep(namespaces, envs, utcnow(), self.grace)
        by_name = {ns.name: ns for ns in namespaces}
        report = SweepReport()

        skips = [a for a in actions if a.kind == "skip_terminating"]
        if skips:
            self._log_skips(skips, by_name, report)

        reaps = [_Reap(a, by_name[a.namespace]) for a in actions if a.kind != "skip_terminating"]
        for reap in reaps:
            action = reap.action
            logger.info("reaping %s (%s): %s", action.namespace, action.kind, action.reason)
            self._delete_cr(reap)
        self._wait_for_crs([r for r in reaps if r.cr is not None])
        for i, reap in enumerate(reaps):
            if self.clock() - started > self.budget:
                report.deferred = [r.action.namespace for r in reaps[i:]]
                logger.warning(
                    "run budget of %ss used up; left for the next run: %s",
                    self.budget,
                    ", ".join(report.deferred),
                )
                break
            self._reap_namespace(reap, report)
        return report

    def _delete_cr(self, reap: _Reap) -> None:
        name = _owning_env(reap.action)
        if name is None:
            return
        try:
            if self.kube.get_env_finalizers(name) is None:
                return
            reap.cr_existed = True
            self.kube.delete_env(name)
            reap.cr = name
        except Exception:
            # The namespace is still deleted in phase 3: it is the resource that leaks.
            logger.exception("could not delete DemoEnvironment %s", name)

    def _wait_for_crs(self, pending: list[_Reap]) -> None:
        """Wait up to `cr_wait` for the CRs to go, then strip the finalizer from stuck ones."""
        deadline = self.clock() + self.cr_wait
        finalizers: dict[str, list[str]] = {}
        while True:
            still: list[_Reap] = []
            for reap in pending:
                assert reap.cr is not None
                try:
                    current = self.kube.get_env_finalizers(reap.cr)
                except Exception:
                    logger.exception("could not read DemoEnvironment %s", reap.cr)
                    still.append(reap)
                    continue
                if current is not None:
                    finalizers[reap.cr] = current
                    still.append(reap)
            pending = still
            if not pending or self.clock() >= deadline:
                break
            self.sleep(self.poll_interval)

        for reap in pending:
            assert reap.cr is not None
            if FINALIZER not in finalizers.get(reap.cr, []):
                continue
            logger.warning(
                "DemoEnvironment %s still has the operator's finalizer after %ss "
                "(operator down?); removing it",
                reap.cr,
                self.cr_wait,
            )
            try:
                reap.finalizer_removed = self.kube.remove_env_finalizer(reap.cr, FINALIZER)
            except Exception:
                logger.exception("could not remove the finalizer from DemoEnvironment %s", reap.cr)

    def _reap_namespace(self, reap: _Reap, report: SweepReport) -> None:
        name = reap.action.namespace
        try:
            outcome = self._guarded_delete(name)
        except Exception:
            logger.exception("could not delete namespace %s", name)
            report.failed.append(name)
            return
        if outcome == "refused":
            logger.error("deletion guard refused namespace %s; not deleting it", name)
            report.refused.append(name)
            return
        logger.info("namespace %s: %s", name, outcome)
        report.reaped.append(name)
        try:
            self._log_reaped(reap, outcome)
        except Exception:
            logger.exception("could not log the reap of %s", name)

    def _guarded_delete(self, name: str) -> NamespaceOutcome:
        """Delete the namespace as it is now, never as it was when the plan was made."""
        for attempt in range(1, DELETE_ATTEMPTS + 1):
            fresh = self.kube.get_namespace(name)
            if fresh is None:
                return "already_gone"
            if fresh.terminating:
                return "already_terminating"
            if not is_deletable_namespace(fresh.name, fresh.labels):
                return "refused"
            try:
                self.kube.delete_namespace(fresh)
            except NamespaceDeletionRefusedError:
                return "refused"
            except ApiException as exc:
                # 409: it changed between the read and the delete (e.g. the
                # garbage collector started deleting it). Re-read and re-check.
                if exc.status == 409 and attempt < DELETE_ATTEMPTS:
                    continue
                raise
            return "deleted"
        raise AssertionError("unreachable")

    def _log_reaped(self, reap: _Reap, outcome: NamespaceOutcome) -> None:
        now = utcnow()
        expires_at = parse_expires_at(reap.ns)
        lag = round((now - expires_at).total_seconds(), 1) if expires_at else None
        self._append(
            EventType.SWEEPER_REAPED,
            reap.ns,
            reap.action,
            {
                "lag_seconds": lag,
                "cr_existed": reap.cr_existed,
                "finalizer_removed": reap.finalizer_removed,
                "namespace_outcome": outcome,
            },
        )

    def _log_skips(
        self, skips: list[SweepAction], by_name: dict[str, NsInfo], report: SweepReport
    ) -> None:
        """Log `sweeper_skipped` once per namespace deletion, not once per run.

        A namespace can sit in Terminating for several runs. The event carries
        the namespace's deletionTimestamp, and one already in the ledger for the
        same namespace and deletionTimestamp is not written again. This reads
        the ledger, so it only runs when something is terminating.
        """
        try:
            logged = {
                (e.namespace, e.details.get("deletion_started_at"))
                for e in self.ledger.read()
                if e.event == EventType.SWEEPER_SKIPPED
            }
        except Exception:
            logger.exception("could not read the ledger; not logging skips this run")
            return
        for action in skips:
            ns = by_name[action.namespace]
            started = to_rfc3339(ns.deletion_started_at) if ns.deletion_started_at else None
            report.skipped.append(ns.name)
            if (ns.name, started) in logged:
                continue
            try:
                self._append(
                    EventType.SWEEPER_SKIPPED, ns, action, {"deletion_started_at": started}
                )
            except Exception:
                logger.exception("could not log the skip of %s", ns.name)

    def _append(
        self, event: EventType, ns: NsInfo, action: SweepAction, extra: dict[str, Any]
    ) -> None:
        self.ledger.append(
            LedgerEvent(
                ts=utcnow(),
                event=event,
                env=action.env or "",
                namespace=ns.name,
                persona=ns.labels.get(LABEL_PERSONA),
                actor="sweeper",
                details={"kind": action.kind, "reason": action.reason, **extra},
            )
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    try:
        load_kube_config()
        sweeper = Sweeper(
            kube=KubeClient(),
            ledger=Ledger(settings.ledger_path),
            grace=timedelta(seconds=settings.sweep_grace),
        )
        report = sweeper.run()
    except Exception:
        logger.exception("sweep aborted before any action was taken")
        return 1
    logger.info(
        "sweep done: reaped=%d skipped=%d refused=%d failed=%d deferred=%d",
        len(report.reaped),
        len(report.skipped),
        len(report.refused),
        len(report.failed),
        len(report.deferred),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
