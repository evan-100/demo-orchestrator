"""TTL lifecycle of a `DemoEnvironment`: extend, expire, and finalizer-backed teardown.

Like `provision`, this is independent of kopf's wiring and of the real
Kubernetes client, so it is unit-tested against a fake facade with a frozen
clock. Every decision reads state from the CR (status, deletionTimestamp) and
the cluster, never from memory, so an operator restart changes nothing.

- `extend`: `spec.ttl` changed. `expiresAt = createdAt + ttl` (spec A3), moved
  in status, on the namespace annotation and in the Crewline banner. An
  invalid TTL can't be reverted (the user owns the spec), so it's reported in
  `status.message` and the old expiry stands.
- `expire`: the periodic timer. Once `expiresAt` has passed, mark the CR
  `Expiring` (so teardown knows the reason), log `expired`, delete the CR.
- `finalize`: the delete handler, which holds the finalizer. It deletes the
  namespace itself (ruling R5: the ownerReference GC would only start after the
  finalizer is released, a deadlock) and waits until it is gone, then logs
  `deleted`. After `DELETE_TIMEOUT` it logs `delete_timeout` and lets go; the
  sweeper reaps whatever is left.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol

import kopf

from orchestrator.core.durations import InvalidDurationError, parse_duration
from orchestrator.core.expiry import (
    TTLExceedsMaxError,
    compute_expires_at,
    from_rfc3339,
    is_expired,
    to_rfc3339,
    utcnow,
    validate_total_ttl,
)
from orchestrator.core.guard import is_deletable_namespace
from orchestrator.core.ledger import EventType, Ledger, LedgerEvent
from orchestrator.core.naming import namespace_for
from orchestrator.core.personas import Persona, UnknownPersonaError, get_persona
from orchestrator.k8s.client import NamespaceInfo

WAIT_DELAY_SECONDS = 3
DELETE_TIMEOUT = timedelta(seconds=120)
EXPIRING = "Expiring"


class LifecycleKube(Protocol):
    """The slice of the Kubernetes API that the TTL lifecycle needs."""

    def get_namespace(self, name: str) -> NamespaceInfo | None:
        """The namespace's current identity and labels, or None if it doesn't exist."""

    def delete_namespace(self, ns: NamespaceInfo) -> None:
        """Delete exactly this namespace (uid/resourceVersion preconditions)."""

    def set_namespace_expiry(self, ns: str, expires_at: str) -> None:
        """Set the namespace's expires-at annotation (no-op if the namespace is absent)."""

    def set_app_expiry(self, ns: str, expires_at: str) -> None:
        """Update the expiry the Crewline app shows (no-op if its Deployment is absent)."""

    def patch_env_status(self, name: str, status: dict[str, Any]) -> None:
        """Merge-patch the DemoEnvironment's status."""

    def delete_env(self, name: str) -> None:
        """Delete the DemoEnvironment (no-op if it is already gone)."""


@dataclass(frozen=True)
class LifecycleDeps:
    kube: LifecycleKube
    ledger: Ledger
    personas: dict[str, Persona]


def _namespace_name(name: str) -> str | None:
    """The env's namespace name, or None if the env name can't form one (so none exists)."""
    try:
        return namespace_for(name)
    except ValueError:
        return None


def _log(
    deps: LifecycleDeps,
    event: EventType,
    *,
    name: str,
    spec: Mapping[str, Any],
    details: dict[str, Any],
) -> None:
    deps.ledger.append(
        LedgerEvent(
            ts=utcnow(),
            event=event,
            env=name,
            namespace=_namespace_name(name) or "",
            persona=spec.get("persona"),
            actor="operator",
            details=details,
        )
    )


def extend(
    *,
    name: str,
    old_ttl: str | None,
    spec: Mapping[str, Any],
    status: Mapping[str, Any],
    patch_status: MutableMapping[str, Any],
    deps: LifecycleDeps,
) -> None:
    """React to a `spec.ttl` change by moving the expiry, or explaining why not."""
    if old_ttl is None or "createdAt" not in status:
        # Initial creation, or still being admitted: the create handler derives
        # the expiry from the current spec.ttl itself.
        return
    if status.get("phase") in ("Failed", EXPIRING):
        # A Failed env keeps its inspection window; an Expiring one is going away.
        return

    try:
        persona = get_persona(deps.personas, str(spec.get("persona", "")))
        ttl = parse_duration(str(spec.get("ttl", "")))
        validate_total_ttl(ttl, persona.max_ttl)
    except UnknownPersonaError as exc:
        patch_status["message"] = str(exc.args[0])
        return
    except (InvalidDurationError, TTLExceedsMaxError) as exc:
        patch_status["message"] = f"TTL change rejected: {exc}"
        return

    old_expires_at = status.get("expiresAt")
    new_expires_at = to_rfc3339(compute_expires_at(from_rfc3339(status["createdAt"]), ttl))
    patch_status.update({"expiresAt": new_expires_at, "message": ""})
    namespace = _namespace_name(name)
    if namespace is not None:
        deps.kube.set_namespace_expiry(namespace, new_expires_at)
        deps.kube.set_app_expiry(namespace, new_expires_at)
    _log(
        deps,
        EventType.EXTENDED,
        name=name,
        spec=spec,
        details={"old_expires_at": old_expires_at, "new_expires_at": new_expires_at},
    )


def expire(
    *,
    name: str,
    spec: Mapping[str, Any],
    status: Mapping[str, Any],
    meta: Mapping[str, Any],
    deps: LifecycleDeps,
) -> None:
    """Timer tick: delete the CR once `status.expiresAt` has passed."""
    expires_at = status.get("expiresAt")
    if not expires_at or meta.get("deletionTimestamp"):
        return
    if not is_expired(from_rfc3339(expires_at), utcnow()):
        return
    if status.get("phase") != EXPIRING:
        # Mark first so the delete handler records reason="expired". If we crash
        # before the delete below, the next tick retries it without a second event.
        deps.kube.patch_env_status(name, {"phase": EXPIRING, "message": "TTL expired"})
        _log(
            deps,
            EventType.EXPIRED,
            name=name,
            spec=spec,
            details={"expires_at": expires_at, "previous_phase": status.get("phase")},
        )
    deps.kube.delete_env(name)


def finalize(
    *,
    name: str,
    spec: Mapping[str, Any],
    status: Mapping[str, Any],
    meta: Mapping[str, Any],
    deps: LifecycleDeps,
) -> None:
    """Delete handler: tear down the namespace, then release the finalizer.

    Raises `kopf.TemporaryError` while the namespace is still terminating.
    """
    now = utcnow()
    reason: Literal["expired", "manual"] = (
        "expired" if status.get("phase") == EXPIRING else "manual"
    )
    # Measured first, from a timestamp stored on the object: once past the
    # timeout the finalizer is released whatever the cluster says, so an API
    # outage or an RBAC gap can never hold a CR in deletion forever.
    elapsed = now - from_rfc3339(meta["deletionTimestamp"])
    timed_out = elapsed > DELETE_TIMEOUT

    def timeout(**extra: Any) -> None:
        _log(
            deps,
            EventType.DELETE_TIMEOUT,
            name=name,
            spec=spec,
            details={
                "elapsed_seconds": round(elapsed.total_seconds(), 1),
                "reason": reason,
                **extra,
            },
        )

    namespace_name = _namespace_name(name)
    try:
        ns = deps.kube.get_namespace(namespace_name) if namespace_name else None
        if ns is not None and not timed_out and not ns.terminating:
            if not is_deletable_namespace(ns.name, ns.labels):
                # Not ours to delete (label missing or tampered with): never touch it.
                timeout(error=f"deletion guard refused namespace {ns.name}")
                return
            deps.kube.delete_namespace(ns)
    except Exception as exc:
        if not timed_out:
            raise  # retried: transient errors in 3 s, others with the handler backoff
        timeout(error=f"{type(exc).__name__}: {exc}")
        return

    if ns is None:
        expires_at = status.get("expiresAt")
        lag = round((now - from_rfc3339(expires_at)).total_seconds(), 1) if expires_at else None
        _log(
            deps,
            EventType.DELETED,
            name=name,
            spec=spec,
            details={"lag_seconds": lag, "reason": reason},
        )
        return

    if timed_out:
        timeout()
        return

    raise kopf.TemporaryError(
        f"waiting for namespace {ns.name} to be deleted", delay=WAIT_DELAY_SECONDS
    )
