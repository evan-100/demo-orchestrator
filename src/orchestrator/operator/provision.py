"""Provisioning logic for a `DemoEnvironment`, independent of the Kubernetes client.

`provision()` is one idempotent reconcile pass. kopf calls it repeatedly (it
raises `kopf.TemporaryError` while waiting on the cluster), and everything it
needs to resume lives in the CR status, so a pass after an operator restart
converges exactly like the next pass would have. The order is:

1. First pass only (no `status.createdAt` yet): record `createdAt`, write the
   `requested` ledger event, and run admission (persona, TTL, capacity). The
   pass ends there (a 1 s `TemporaryError`) so the status is persisted before
   any cluster work.
2. Apply the namespace, then every workload (server-side apply, idempotent).
3. Wait for the postgres Deployment, then enter `Seeding` and apply the seed
   Job. The seed has no DB retry of its own, so it must not start earlier.
4. Wait for the seed Job to succeed.
5. Wait for the crewline Deployment. Its readiness probe is `/readyz`, which
   needs the seed marker, so crewline cannot become available before step 4.
6. `Ready`: `readyAt`, `url`, `timings` and a `ready` ledger event.

Phase start times are wall-clock timestamps kept in `status.checkpoints`
(ruling R4), so timings survive restarts. Failures (admission, seed Job,
provision timeout) set `Failed`, keep the namespace inspectable for 10 minutes
and raise `kopf.PermanentError`.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, NoReturn, Protocol

import kopf

from orchestrator.constants import NS_PREFIX
from orchestrator.core.durations import InvalidDurationError, parse_duration
from orchestrator.core.expiry import (
    TTLExceedsMaxError,
    compute_expires_at,
    from_rfc3339,
    to_rfc3339,
    utcnow,
    validate_total_ttl,
)
from orchestrator.core.ledger import EventType, Ledger, LedgerEvent
from orchestrator.core.naming import namespace_for
from orchestrator.core.personas import Persona, UnknownPersonaError, get_persona
from orchestrator.k8s.manifests import (
    EnvContext,
    render_namespace,
    render_seed_job,
    render_workloads,
    url_for,
)

WAIT_DELAY_SECONDS = 3
ADMITTED_DELAY_SECONDS = 1
FAILED_INSPECTION_WINDOW = timedelta(minutes=10)

POSTGRES_DEPLOYMENT = "postgres"
CREWLINE_DEPLOYMENT = "crewline"
SEED_JOB = "seed"

# Keys under `status.checkpoints`: the wall-clock start of each stage.
CP_PROVISIONING = "provisioningAt"  # first namespace apply began
CP_WORKLOADS = "workloadsAppliedAt"  # namespace + workloads applied
CP_SEEDING = "seedingAt"  # postgres available, seed Job applied
CP_SEEDED = "seededAt"  # seed Job succeeded

JobState = Literal["running", "succeeded", "failed"]


class KubeFacade(Protocol):
    """The slice of the Kubernetes API that provisioning needs."""

    def apply(self, manifest: dict[str, Any]) -> None:
        """Server-side apply `manifest` with fieldManager="demo-orchestrator"."""

    def deployment_available(self, ns: str, name: str) -> bool:
        """True once the Deployment has at least one available replica."""

    def job_status(self, ns: str, name: str) -> JobState:
        """The Job's outcome so far."""

    def count_active_envs(self, exclude: str) -> int:
        """Count DemoEnvironments that are not `Failed`, excluding the one named `exclude`."""

    def set_namespace_expiry(self, ns: str, expires_at: str) -> None:
        """Set the namespace's expires-at annotation (no-op if the namespace is absent)."""


class AdmissionError(Exception):
    """A request that must be rejected. `message` is shown to the user verbatim."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ProvisionDeps:
    kube: KubeFacade
    ledger: Ledger
    personas: dict[str, Persona]
    max_envs: int
    provision_timeout: timedelta
    base_domain: str


def resolve_request(
    spec: Mapping[str, Any], personas: dict[str, Persona]
) -> tuple[Persona, timedelta]:
    """Validate the spec's persona and TTL, raising `AdmissionError` with a readable message."""
    try:
        persona = get_persona(personas, str(spec.get("persona", "")))
    except UnknownPersonaError as exc:
        raise AdmissionError(str(exc.args[0])) from None
    try:
        ttl = parse_duration(str(spec.get("ttl", "")))
        validate_total_ttl(ttl, persona.max_ttl)
    except (InvalidDurationError, TTLExceedsMaxError) as exc:
        raise AdmissionError(str(exc)) from None
    return persona, ttl


def check_admission(
    spec: Mapping[str, Any], personas: dict[str, Persona], active: int, max_envs: int
) -> tuple[Persona, timedelta]:
    """Admit or reject a new environment request.

    Request errors (unknown persona, bad TTL, TTL over the persona max) are
    reported before capacity, since they would fail regardless of load.
    """
    persona, ttl = resolve_request(spec, personas)
    if active >= max_envs:
        raise AdmissionError(f"capacity: {active}/{max_envs} environments in use")
    return persona, ttl


def _seconds(start: str, end: str) -> float:
    return round((from_rfc3339(end) - from_rfc3339(start)).total_seconds(), 1)


def compute_timings(checkpoints: Mapping[str, str], ready_at: str) -> dict[str, float]:
    """Stage durations from the stored checkpoints.

    `appReadySeconds` is the time spent waiting on the app's Deployments:
    postgres before seeding plus crewline after it. The three stages sum to
    `totalSeconds` (first apply to Ready).
    """
    return {
        "namespaceSeconds": _seconds(checkpoints[CP_PROVISIONING], checkpoints[CP_WORKLOADS]),
        "appReadySeconds": round(
            _seconds(checkpoints[CP_WORKLOADS], checkpoints[CP_SEEDING])
            + _seconds(checkpoints[CP_SEEDED], ready_at),
            1,
        ),
        "seedSeconds": _seconds(checkpoints[CP_SEEDING], checkpoints[CP_SEEDED]),
        "totalSeconds": _seconds(checkpoints[CP_PROVISIONING], ready_at),
    }


class _Pass:
    """State for one reconcile pass: what we know and what we are patching."""

    def __init__(
        self,
        *,
        name: str,
        namespace: str,
        spec: Mapping[str, Any],
        status: Mapping[str, Any],
        patch_status: MutableMapping[str, Any],
        deps: ProvisionDeps,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self.spec = spec
        self.status = status
        self.patch_status = patch_status
        self.deps = deps
        self.checkpoints: dict[str, str] = dict(status.get("checkpoints") or {})

    def record(self, key: str) -> None:
        self.checkpoints[key] = to_rfc3339(utcnow())
        self.patch_status["checkpoints"] = dict(self.checkpoints)

    def log(self, event: EventType, details: dict[str, Any] | None = None) -> None:
        self.deps.ledger.append(
            LedgerEvent(
                ts=utcnow(),
                event=event,
                env=self.name,
                namespace=self.namespace,
                persona=self.spec.get("persona"),
                actor="operator",
                details=details or {},
            )
        )

    def wait(self, reason: str) -> NoReturn:
        raise kopf.TemporaryError(reason, delay=WAIT_DELAY_SECONDS)

    def fail(self, message: str, *, namespace_exists: bool) -> NoReturn:
        expires_at = to_rfc3339(utcnow() + FAILED_INSPECTION_WINDOW)
        if namespace_exists:
            self.deps.kube.set_namespace_expiry(self.namespace, expires_at)
        self.patch_status.update({"phase": "Failed", "message": message, "expiresAt": expires_at})
        self.log(EventType.FAILED, {"reason": message})
        raise kopf.PermanentError(message)


def provision(
    *,
    name: str,
    uid: str,
    spec: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    creation_timestamp: str,
    patch_status: MutableMapping[str, Any],
    deps: ProvisionDeps,
) -> None:
    """Run one idempotent provisioning pass for the DemoEnvironment `name`.

    Writes status changes into `patch_status` (kopf's `patch.status`) and
    raises `kopf.TemporaryError` to be called again, `kopf.PermanentError` on
    failure, or returns once the environment is Ready.
    """
    status = status or {}
    if status.get("phase") in ("Ready", "Failed", "Expiring"):
        return

    try:
        namespace = namespace_for(name)
        name_error = None
    except ValueError as exc:
        namespace, name_error = f"{NS_PREFIX}{name}", str(exc)
    p = _Pass(
        name=name,
        namespace=namespace,
        spec=spec,
        status=status,
        patch_status=patch_status,
        deps=deps,
    )
    created_at = from_rfc3339(creation_timestamp)

    if "createdAt" not in status:
        # First pass: admit and persist, touching nothing in the cluster yet.
        # Ending the pass here lets kopf write `createdAt` before any cluster
        # work, so a crash after this point never repeats the `requested` event.
        patch_status.update(
            {"phase": "Provisioning", "createdAt": to_rfc3339(created_at), "message": ""}
        )
        p.log(EventType.REQUESTED, {"ttl": spec.get("ttl"), "requestedBy": spec.get("requestedBy")})
        if name_error is not None:
            p.fail(name_error, namespace_exists=False)
        try:
            _, ttl = check_admission(
                spec, deps.personas, deps.kube.count_active_envs(exclude=name), deps.max_envs
            )
        except AdmissionError as exc:
            p.fail(exc.message, namespace_exists=False)
        patch_status.update(
            {
                "namespace": p.namespace,
                "expiresAt": to_rfc3339(compute_expires_at(created_at, ttl)),
            }
        )
        raise kopf.TemporaryError(
            "waiting for the admission result to be persisted", delay=ADMITTED_DELAY_SECONDS
        )

    namespace_exists = CP_PROVISIONING in p.checkpoints
    try:
        persona = get_persona(deps.personas, str(spec.get("persona", "")))
    except UnknownPersonaError as exc:
        p.fail(str(exc.args[0]), namespace_exists=namespace_exists)

    if utcnow() - created_at > deps.provision_timeout:
        p.fail(
            f"provision timeout: not Ready within {int(deps.provision_timeout.total_seconds())}s",
            namespace_exists=namespace_exists,
        )

    # `spec.ttl` may change while we provision (kopf only reports it as a field
    # change once creation is done), so the expiry follows the current spec. An
    # invalid new TTL can't be reverted: keep the admitted expiry and say why.
    ttl_error = None
    try:
        ttl = parse_duration(str(spec.get("ttl", "")))
        validate_total_ttl(ttl, persona.max_ttl)
        expires_at = compute_expires_at(created_at, ttl)
    except (InvalidDurationError, TTLExceedsMaxError) as exc:
        if "expiresAt" not in status:
            p.fail(str(exc), namespace_exists=namespace_exists)
        ttl_error = f"TTL change rejected: {exc}"
        expires_at = from_rfc3339(status["expiresAt"])
        patch_status["message"] = ttl_error
    patch_status["expiresAt"] = to_rfc3339(expires_at)
    if CP_WORKLOADS in p.checkpoints and status.get("expiresAt") != patch_status["expiresAt"]:
        deps.kube.set_namespace_expiry(p.namespace, patch_status["expiresAt"])

    ctx = EnvContext(
        env_name=name,
        namespace=p.namespace,
        persona=persona,
        expires_at=expires_at,
        owner_uid=uid,
        base_domain=deps.base_domain,
    )

    if CP_WORKLOADS not in p.checkpoints:
        if CP_PROVISIONING not in p.checkpoints:
            p.record(CP_PROVISIONING)
        deps.kube.apply(render_namespace(ctx))
        for manifest in render_workloads(ctx):
            deps.kube.apply(manifest)
        p.record(CP_WORKLOADS)

    if CP_SEEDING not in p.checkpoints:
        if not deps.kube.deployment_available(p.namespace, POSTGRES_DEPLOYMENT):
            p.wait("waiting for the postgres Deployment to become available")
        deps.kube.apply(render_seed_job(ctx))
        p.record(CP_SEEDING)
        patch_status["phase"] = "Seeding"

    if CP_SEEDED not in p.checkpoints:
        job = deps.kube.job_status(p.namespace, SEED_JOB)
        if job == "failed":
            p.fail("seed job failed", namespace_exists=True)
        if job == "running":
            p.wait("waiting for the seed Job to succeed")
        p.record(CP_SEEDED)

    if not deps.kube.deployment_available(p.namespace, CREWLINE_DEPLOYMENT):
        p.wait("waiting for the crewline Deployment to become available")

    ready_at = to_rfc3339(utcnow())
    timings = compute_timings(p.checkpoints, ready_at)
    patch_status.update(
        {
            "phase": "Ready",
            "readyAt": ready_at,
            "url": url_for(ctx),
            "timings": timings,
            "message": ttl_error or "",
        }
    )
    p.log(
        EventType.READY,
        {"provisioning_seconds": timings["totalSeconds"], "timings": timings},
    )
