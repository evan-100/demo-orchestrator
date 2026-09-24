"""kopf handlers for `DemoEnvironment` resources.

Run with `kopf run -m orchestrator.operator.handlers --all-namespaces`.
Handlers are thin: they wire settings and the Kubernetes client into the
logic modules (`provision`, and later expiry/extend/delete), which are
unit-tested against a fake facade.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from typing import Any

import kopf

from orchestrator.config import get_settings
from orchestrator.constants import GROUP, PLURAL, VERSION
from orchestrator.core.ledger import Ledger
from orchestrator.core.personas import load_personas
from orchestrator.k8s.client import KubeClient, load_kube_config
from orchestrator.operator.provision import ProvisionDeps, provision


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
    settings.posting.level = logging.WARNING


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
    provision(
        name=name,
        uid=uid,
        spec=dict(spec),
        status=dict(status),
        creation_timestamp=meta["creationTimestamp"],
        patch_status=patch.status,
        deps=_deps(),
    )
