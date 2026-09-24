"""kopf handlers for `DemoEnvironment` resources.

Run with `kopf run -m orchestrator.operator.handlers --all-namespaces`.
Handlers are thin: they wire settings and the Kubernetes client into the
logic modules (`provision` and `lifecycle`), which are unit-tested against a
fake facade.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from functools import lru_cache
from typing import Any

import kopf
import urllib3
from kubernetes.client.exceptions import ApiException

from orchestrator.config import get_settings
from orchestrator.constants import GROUP, PLURAL, VERSION
from orchestrator.core.ledger import Ledger
from orchestrator.core.personas import load_personas
from orchestrator.k8s.client import KubeClient, load_kube_config
from orchestrator.operator.lifecycle import LifecycleDeps, expire, extend, finalize
from orchestrator.operator.provision import WAIT_DELAY_SECONDS, ProvisionDeps, provision

EXPIRY_CHECK_INTERVAL_SECONDS = 10
TRANSIENT_STATUSES = frozenset({409, 429})
TRANSIENT_PREFIX = "transient API error"
# kopf-level backstop for the finalizer, beyond lifecycle.DELETE_TIMEOUT (120 s).
# When it hits, kopf marks the handler failed (HandlerTimeoutError), which counts
# as finished, so the finalizer is still removed. backoff=3 keeps kopf's 60 s
# default from both slowing retries and triggering its timeout lookahead early.
DELETE_HANDLER_TIMEOUT_SECONDS = 150


@lru_cache
def _kube() -> KubeClient:
    load_kube_config()
    return KubeClient()


def _deps() -> ProvisionDeps:
    settings = get_settings()
    return ProvisionDeps(
        kube=_kube(),
        ledger=Ledger(settings.ledger_path),
        personas=load_personas(settings.personas_dir),
        max_envs=settings.max_concurrent_envs,
        provision_timeout=timedelta(seconds=settings.provision_timeout),
        base_domain=settings.base_domain,
    )


def _lifecycle_deps() -> LifecycleDeps:
    settings = get_settings()
    return LifecycleDeps(
        kube=_kube(),
        ledger=Ledger(settings.ledger_path),
        personas=load_personas(settings.personas_dir),
    )


@contextmanager
def _transient_api_errors() -> Iterator[None]:
    """Retry API hiccups (5xx, 409, 429, connection errors) in 3 s, not kopf's 60 s backoff."""
    try:
        yield
    except ApiException as exc:
        if exc.status in TRANSIENT_STATUSES or (exc.status or 0) >= 500:
            raise kopf.TemporaryError(
                f"{TRANSIENT_PREFIX}: {exc.status} {exc.reason}", delay=WAIT_DELAY_SECONDS
            ) from exc
        raise
    except urllib3.exceptions.HTTPError as exc:
        raise kopf.TemporaryError(f"{TRANSIENT_PREFIX}: {exc}", delay=WAIT_DELAY_SECONDS) from exc


class QuietWaitsFilter(logging.Filter):
    """Keep routine waits out of k8s Events and ERROR logs, without hiding failures.

    kopf reports every `TemporaryError` as an ERROR record on the `kopf.objects`
    logger ("... failed temporarily: <message>"), and its Event poster sees the
    same record. Polling the cluster every 3 s is how provisioning and teardown
    wait, not an error, so records whose message is one of our waits ("waiting
    for ...") become DEBUG and are marked `k8s_skip` (kopf's own "don't post"
    flag). Transient API errors become WARNINGs. Everything else (notably
    "failed permanently") is left as kopf emitted it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "failed temporarily: waiting for" in message:
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
            record.k8s_skip = True
            # Handlers have no level of their own, so re-apply the logger's.
            return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        if f"failed temporarily: {TRANSIENT_PREFIX}" in message:
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    # Handler progress lives on the CR itself (annotations), so a restarted
    # operator resumes where it left off. Annotations rather than `status.kopf`
    # because the CRD's structural schema would prune unknown status fields.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=GROUP)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix=GROUP, key="last-handled-configuration"
    )
    settings.persistence.finalizer = f"{GROUP}/finalizer"
    # Warnings and errors (e.g. "failed permanently") are posted as k8s Events;
    # routine waits are demoted below that by QuietWaitsFilter.
    settings.posting.level = logging.WARNING
    objects_logger = logging.getLogger("kopf.objects")
    if not any(isinstance(f, QuietWaitsFilter) for f in objects_logger.filters):
        objects_logger.addFilter(QuietWaitsFilter())


@kopf.on.create(GROUP, VERSION, PLURAL)
def on_create(
    name: str,
    uid: str,
    spec: kopf.Spec | dict[str, Any],
    status: kopf.Status | dict[str, Any],
    meta: kopf.Meta | dict[str, Any],
    patch: kopf.Patch,
    **_: Any,
) -> None:
    with _transient_api_errors():
        provision(
            name=name,
            uid=uid,
            spec=dict(spec),
            status=dict(status),
            creation_timestamp=meta["creationTimestamp"],
            patch_status=patch.status,
            deps=_deps(),
        )


@kopf.on.field(GROUP, VERSION, PLURAL, field="spec.ttl")
def on_ttl_change(
    name: str,
    old: Any,
    new: Any,
    spec: kopf.Spec | dict[str, Any],
    status: kopf.Status | dict[str, Any],
    patch: kopf.Patch,
    **_: Any,
) -> None:
    with _transient_api_errors():
        extend(
            name=name,
            old_ttl=old,
            spec=dict(spec),
            status=dict(status),
            patch_status=patch.status,
            deps=_lifecycle_deps(),
        )


@kopf.timer(GROUP, VERSION, PLURAL, interval=EXPIRY_CHECK_INTERVAL_SECONDS, idle=0)
def expiry_timer(
    name: str,
    spec: kopf.Spec | dict[str, Any],
    status: kopf.Status | dict[str, Any],
    meta: kopf.Meta | dict[str, Any],
    **_: Any,
) -> None:
    with _transient_api_errors():
        expire(
            name=name,
            spec=dict(spec),
            status=dict(status),
            meta=dict(meta),
            deps=_lifecycle_deps(),
        )


@kopf.on.delete(
    GROUP,
    VERSION,
    PLURAL,
    timeout=DELETE_HANDLER_TIMEOUT_SECONDS,
    backoff=WAIT_DELAY_SECONDS,
)
def on_delete(
    name: str,
    spec: kopf.Spec | dict[str, Any],
    status: kopf.Status | dict[str, Any],
    meta: kopf.Meta | dict[str, Any],
    **_: Any,
) -> None:
    with _transient_api_errors():
        finalize(
            name=name,
            spec=dict(spec),
            status=dict(status),
            meta=dict(meta),
            deps=_lifecycle_deps(),
        )
