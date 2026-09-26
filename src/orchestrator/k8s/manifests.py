"""Render the Kubernetes manifests for a single demo environment.

Each resource is a Jinja2 template under `k8s/templates/` rendered with
`StrictUndefined` (a missing context variable is a bug, not a blank), then
parsed back with `yaml.safe_load_all` so callers (Task 7's operator) get
plain dicts ready for the Kubernetes client — never raw YAML text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from orchestrator.constants import (
    ANNOTATION_EXPIRES_AT,
    GROUP,
    KIND,
    LABEL_ENV,
    LABEL_MANAGED_BY,
    LABEL_PERSONA,
    MANAGED_BY_VALUE,
    VERSION,
)
from orchestrator.core.durations import format_duration
from orchestrator.core.expiry import to_rfc3339
from orchestrator.core.personas import Persona

# Pinned at build time: `postgres:16-alpine` per spec A3, resolved to the
# current 16.x-alpine patch tag (verified against Docker Hub's tags API).
POSTGRES_IMAGE = "postgres:16.15-alpine"

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# The Crewline Deployment/container name and the env var holding the expiry the
# app shows in its banner (crewline.yaml.j2). The operator patches that env var
# when the expiry moves after the first apply.
CREWLINE_NAME = "crewline"
APP_EXPIRES_AT_ENV = "EXPIRES_AT"

_WORKLOAD_TEMPLATES = (
    "quota.yaml.j2",
    "limitrange.yaml.j2",
    "networkpolicy.yaml.j2",
    "persona-configmap.yaml.j2",
    "postgres.yaml.j2",
    "crewline.yaml.j2",
    "ingress.yaml.j2",
)


def _make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


_JINJA_ENV = _make_env()


@dataclass(frozen=True)
class EnvContext:
    """Everything a template needs to render one demo environment's manifests."""

    env_name: str
    namespace: str
    persona: Persona
    expires_at: datetime
    owner_uid: str
    base_domain: str
    crewline_image: str = "crewline:dev"


def url_for(ctx: EnvContext) -> str:
    """The externally reachable URL for this environment's Crewline ingress."""
    return f"http://{ctx.env_name}.{ctx.base_domain}"


def _postgres_password(ctx: EnvContext) -> str:
    """Deterministic per-namespace Postgres password.

    Derived from `owner_uid` (the DemoEnvironment CR's UID) via SHA-256 rather
    than `secrets.token_urlsafe`, so re-rendering the same environment (e.g.
    after an operator restart, per global-constraints "state lives in the
    cluster") always produces the same Secret and re-applying is a no-op.
    The namespace is isolated and ephemeral, so a derived-not-random demo
    password is an acceptable tradeoff (see task context).
    """
    digest = hashlib.sha256(f"{ctx.owner_uid}:{ctx.namespace}".encode()).hexdigest()
    return digest[:32]


def _persona_yaml(persona: Persona) -> str:
    """Serialize `persona` back into the on-disk `persona.yaml` shape.

    Chosen approach: rebuild the mapping from the validated `Persona` model
    (formatting `default_ttl`/`max_ttl` back to compact h/m/s strings with
    `format_duration`) rather than threading the original file's raw text
    through `EnvContext`, which only carries the parsed model — this is the
    simplest option that doesn't require widening the render interface, and
    round-trips losslessly since `Persona` already validates the same shape.
    """
    data = {
        "name": persona.name,
        "display_name": persona.display_name,
        "brand": {
            "company_name": persona.brand.company_name,
            "primary_color": persona.brand.primary_color,
        },
        "default_ttl": format_duration(persona.default_ttl),
        "max_ttl": format_duration(persona.max_ttl),
        "resources": {
            "cpu": persona.resources.cpu,
            "memory": persona.resources.memory,
            "pods": persona.resources.pods,
        },
        "fixtures": {
            "seed": persona.fixtures.seed,
            "locations": persona.fixtures.locations,
            "employees": persona.fixtures.employees,
            "roles": persona.fixtures.roles,
            "certifications": persona.fixtures.certifications,
            "shift_pattern": persona.fixtures.shift_pattern,
        },
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _render_context(ctx: EnvContext) -> dict[str, Any]:
    return {
        "group": GROUP,
        "version": VERSION,
        "kind": KIND,
        "label_managed_by": LABEL_MANAGED_BY,
        "managed_by_value": MANAGED_BY_VALUE,
        "label_env": LABEL_ENV,
        "label_persona": LABEL_PERSONA,
        "annotation_expires_at": ANNOTATION_EXPIRES_AT,
        "env_name": ctx.env_name,
        "namespace": ctx.namespace,
        "persona": ctx.persona,
        "persona_yaml": _persona_yaml(ctx.persona),
        "expires_at": to_rfc3339(ctx.expires_at),
        "owner_uid": ctx.owner_uid,
        "base_domain": ctx.base_domain,
        "crewline_image": ctx.crewline_image,
        "postgres_image": POSTGRES_IMAGE,
        "postgres_password": _postgres_password(ctx),
    }


def _render_docs(template_name: str, ctx: EnvContext) -> list[dict[str, Any]]:
    """Render one template and parse it as one or more YAML documents."""
    text = _JINJA_ENV.get_template(template_name).render(**_render_context(ctx))
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]


def _render_single(template_name: str, ctx: EnvContext) -> dict[str, Any]:
    docs = _render_docs(template_name, ctx)
    if len(docs) != 1:
        raise ValueError(f"{template_name} rendered {len(docs)} documents, expected exactly 1")
    return docs[0]


def render_namespace(ctx: EnvContext) -> dict[str, Any]:
    """Render the per-demo `Namespace`: labels, expires-at annotation, ownerReference."""
    return _render_single("namespace.yaml.j2", ctx)


def render_workloads(ctx: EnvContext) -> list[dict[str, Any]]:
    """Render every namespaced resource except the Namespace itself and the seed Job.

    Returned in apply order: quota and limit range first (so pods are always
    admitted under a quota), then network policy, the persona ConfigMap,
    Postgres, Crewline, and finally the Ingress.
    """
    workloads: list[dict[str, Any]] = []
    for template_name in _WORKLOAD_TEMPLATES:
        workloads.extend(_render_docs(template_name, ctx))
    return workloads


def render_seed_job(ctx: EnvContext) -> dict[str, Any]:
    """Render the seed `Job`. Applied last, once Postgres is ready."""
    return _render_single("seed-job.yaml.j2", ctx)


__all__ = [
    "APP_EXPIRES_AT_ENV",
    "CREWLINE_NAME",
    "EnvContext",
    "render_namespace",
    "render_seed_job",
    "render_workloads",
    "url_for",
]
