"""Tests for CRD offline validation and per-demo manifest rendering."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

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
from orchestrator.core.expiry import to_rfc3339
from orchestrator.core.personas import Persona, load_persona_file
from orchestrator.k8s.manifests import (
    APP_EXPIRES_AT_ENV,
    CREWLINE_NAME,
    EnvContext,
    render_namespace,
    render_seed_job,
    render_workloads,
    url_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = REPO_ROOT / "personas"
CRD_PATH = REPO_ROOT / "deploy" / "crd" / "demoenvironments.yaml"

# The exact TTL grammar from global-constraints.md.
TTL_REGEX = r"^([0-9]+h)?([0-9]+m)?([0-9]+s)?$"


@pytest.fixture
def persona() -> Persona:
    return load_persona_file(PERSONAS_DIR / "healthcare" / "persona.yaml")


@pytest.fixture
def ctx(persona: Persona) -> EnvContext:
    return EnvContext(
        env_name="healthcare-ab12",
        namespace="demo-healthcare-ab12",
        persona=persona,
        expires_at=datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=UTC),
        owner_uid="a1b2c3d4-e5f6-7890-abcd-ef0123456789",
        base_domain="demo.localtest.me",
    )


def _containers(deployment_or_job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(deployment_or_job["spec"]["template"]["spec"]["containers"])


# --- Namespace ---------------------------------------------------------


def test_namespace_has_all_three_labels(ctx: EnvContext) -> None:
    ns = render_namespace(ctx)
    labels = ns["metadata"]["labels"]
    assert labels[LABEL_MANAGED_BY] == MANAGED_BY_VALUE
    assert labels[LABEL_ENV] == ctx.env_name
    assert labels[LABEL_PERSONA] == ctx.persona.name


def test_namespace_annotation_equals_to_rfc3339(ctx: EnvContext) -> None:
    ns = render_namespace(ctx)
    assert ns["metadata"]["annotations"][ANNOTATION_EXPIRES_AT] == to_rfc3339(ctx.expires_at)


def test_namespace_owner_reference(ctx: EnvContext) -> None:
    ns = render_namespace(ctx)
    owner_refs = ns["metadata"]["ownerReferences"]
    assert len(owner_refs) == 1
    ref = owner_refs[0]
    assert ref["apiVersion"] == f"{GROUP}/{VERSION}"
    assert ref["kind"] == KIND
    assert ref["name"] == ctx.env_name
    assert ref["uid"] == ctx.owner_uid
    assert ref["controller"] is True
    assert ref["blockOwnerDeletion"] is True


def test_namespace_name(ctx: EnvContext) -> None:
    ns = render_namespace(ctx)
    assert ns["metadata"]["name"] == ctx.namespace
    assert "namespace" not in ns["metadata"]  # a Namespace is not itself namespaced


@pytest.mark.parametrize("name", ["1234", "1e3", "yes", "on", "null", "true"])
def test_yaml_ambiguous_names_render_as_strings(persona: Persona, name: str) -> None:
    """Valid CR/persona names that YAML would read as int/float/bool/null stay strings."""
    odd_persona = persona.model_copy(update={"name": name})
    ctx = EnvContext(
        env_name=name,
        namespace=f"demo-{name}",
        persona=odd_persona,
        expires_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        owner_uid="12345678",
        base_domain="demo.localtest.me",
    )
    ns = render_namespace(ctx)
    assert ns["metadata"]["labels"][LABEL_ENV] == name
    assert ns["metadata"]["labels"][LABEL_PERSONA] == name
    assert ns["metadata"]["ownerReferences"][0]["name"] == name
    assert ns["metadata"]["ownerReferences"][0]["uid"] == "12345678"
    pod_labels = [
        doc["spec"]["template"]["metadata"]["labels"][LABEL_ENV]
        for doc in [*render_workloads(ctx), render_seed_job(ctx)]
        if doc["kind"] in ("Deployment", "Job")
    ]
    assert pod_labels and all(label == name for label in pod_labels)


# --- Workloads: namespace scoping --------------------------------------


def test_every_workload_is_scoped_to_the_namespace(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    assert workloads, "expected at least one rendered workload"
    for doc in workloads:
        assert doc["metadata"]["namespace"] == ctx.namespace, doc


def test_seed_job_is_scoped_to_the_namespace(ctx: EnvContext) -> None:
    job = render_seed_job(ctx)
    assert job["metadata"]["namespace"] == ctx.namespace


def test_workloads_excludes_namespace_and_seed_job(ctx: EnvContext) -> None:
    kinds = {doc["kind"] for doc in render_workloads(ctx)}
    assert "Namespace" not in kinds
    assert "Job" not in kinds


# --- ResourceQuota / LimitRange -----------------------------------------


def test_resource_quota_matches_persona_resources(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    quota = next(d for d in workloads if d["kind"] == "ResourceQuota")
    hard = quota["spec"]["hard"]
    assert hard["requests.cpu"] == ctx.persona.resources.cpu
    assert hard["limits.cpu"] == ctx.persona.resources.cpu
    assert hard["requests.memory"] == ctx.persona.resources.memory
    assert hard["limits.memory"] == ctx.persona.resources.memory
    assert int(hard["pods"]) == ctx.persona.resources.pods


def test_limit_range_max_within_persona_resources(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    limit_range = next(d for d in workloads if d["kind"] == "LimitRange")
    container_limits = limit_range["spec"]["limits"][0]
    assert container_limits["max"]["cpu"] == ctx.persona.resources.cpu
    assert container_limits["max"]["memory"] == ctx.persona.resources.memory


# --- NetworkPolicy -------------------------------------------------------


def test_network_policy_default_deny_present(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    policies = [d for d in workloads if d["kind"] == "NetworkPolicy"]
    assert len(policies) == 3
    deny = next(p for p in policies if p["metadata"]["name"] == "default-deny-ingress")
    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["policyTypes"] == ["Ingress"]
    assert "ingress" not in deny["spec"]


def test_network_policy_allows_ingress_nginx_to_crewline(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    policies = [d for d in workloads if d["kind"] == "NetworkPolicy"]
    allow = next(p for p in policies if p["metadata"]["name"] == "allow-ingress-to-crewline")
    assert allow["spec"]["podSelector"]["matchLabels"] == {"app": "crewline"}
    rule = allow["spec"]["ingress"][0]
    assert rule["from"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "ingress-nginx"
    }
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_network_policy_allows_app_to_postgres(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    policies = [d for d in workloads if d["kind"] == "NetworkPolicy"]
    allow = next(p for p in policies if p["metadata"]["name"] == "allow-app-to-postgres")
    assert allow["spec"]["podSelector"]["matchLabels"] == {"app": "postgres"}
    rule = allow["spec"]["ingress"][0]
    sources = {frozenset(f["podSelector"]["matchLabels"].items()) for f in rule["from"]}
    assert frozenset({"app": "crewline"}.items()) in sources
    assert frozenset({"app": "seed"}.items()) in sources
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]


# --- Ingress ---------------------------------------------------------------


def test_ingress_host(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    ingress = next(d for d in workloads if d["kind"] == "Ingress")
    assert ingress["spec"]["rules"][0]["host"] == f"{ctx.env_name}.demo.localtest.me"
    assert ingress["spec"]["ingressClassName"] == "nginx"


def test_url_for(ctx: EnvContext) -> None:
    assert url_for(ctx) == f"http://{ctx.env_name}.demo.localtest.me"


# --- Crewline Deployment ------------------------------------------------


def test_crewline_probes_and_env(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    deployment = next(
        d for d in workloads if d["kind"] == "Deployment" and d["metadata"]["name"] == "crewline"
    )
    container = _containers(deployment)[0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    env_names = {e["name"] for e in container["env"]}
    assert {"DATABASE_URL", "PERSONA_FILE", "EXPIRES_AT"} <= env_names
    expires_at_entry = next(e for e in container["env"] if e["name"] == "EXPIRES_AT")
    assert expires_at_entry["value"] == to_rfc3339(ctx.expires_at)


def test_app_expiry_patch_target_matches_the_rendered_crewline_deployment(
    ctx: EnvContext,
) -> None:
    """`KubeClient.set_app_expiry` patches by these names; they must match the template."""
    (deployment,) = [
        d
        for d in render_workloads(ctx)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == CREWLINE_NAME
    ]
    container = next(c for c in _containers(deployment) if c["name"] == CREWLINE_NAME)
    assert any(e["name"] == APP_EXPIRES_AT_ENV for e in container["env"])


def test_crewline_image_defaults_and_is_overridable(persona: Persona) -> None:
    ctx = EnvContext(
        env_name="healthcare-zz99",
        namespace="demo-healthcare-zz99",
        persona=persona,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        owner_uid="uid-1",
        base_domain="demo.localtest.me",
        crewline_image="registry.local/crewline:1.2.3",
    )
    workloads = render_workloads(ctx)
    deployment = next(
        d for d in workloads if d["kind"] == "Deployment" and d["metadata"]["name"] == "crewline"
    )
    assert _containers(deployment)[0]["image"] == "registry.local/crewline:1.2.3"


# --- Every container has requests AND limits ----------------------------


def _all_containers(ctx: EnvContext) -> list[dict[str, Any]]:
    containers = []
    for doc in render_workloads(ctx):
        if doc["kind"] in ("Deployment",):
            containers.extend(_containers(doc))
    containers.extend(_containers(render_seed_job(ctx)))
    return containers


def test_every_container_has_requests_and_limits(ctx: EnvContext) -> None:
    containers = _all_containers(ctx)
    assert len(containers) == 3  # postgres, crewline, seed
    for container in containers:
        resources = container.get("resources")
        assert resources, f"{container['name']} is missing resources entirely"
        assert resources.get("requests"), f"{container['name']} is missing resource requests"
        assert resources.get("limits"), f"{container['name']} is missing resource limits"
        for block in ("requests", "limits"):
            assert "cpu" in resources[block], container["name"]
            assert "memory" in resources[block], container["name"]


def test_container_resource_sums_fit_within_persona_quota(ctx: EnvContext) -> None:
    """A real gotcha per the brief: quota rejects pods if this doesn't hold."""

    def _cpu_to_millis(value: str) -> int:
        return int(value[:-1]) if value.endswith("m") else int(value) * 1000

    def _mem_to_mi(value: str) -> int:
        if value.endswith("Gi"):
            return int(value[:-2]) * 1024
        assert value.endswith("Mi")
        return int(value[:-2])

    containers = _all_containers(ctx)
    total_request_cpu = sum(_cpu_to_millis(c["resources"]["requests"]["cpu"]) for c in containers)
    total_limit_cpu = sum(_cpu_to_millis(c["resources"]["limits"]["cpu"]) for c in containers)
    total_request_mem = sum(_mem_to_mi(c["resources"]["requests"]["memory"]) for c in containers)
    total_limit_mem = sum(_mem_to_mi(c["resources"]["limits"]["memory"]) for c in containers)

    quota_cpu_millis = _cpu_to_millis(ctx.persona.resources.cpu)
    quota_mem_mi = _mem_to_mi(ctx.persona.resources.memory)

    assert total_request_cpu <= quota_cpu_millis
    assert total_limit_cpu <= quota_cpu_millis
    assert total_request_mem <= quota_mem_mi
    assert total_limit_mem <= quota_mem_mi


# --- Persona ConfigMap ----------------------------------------------------


def test_persona_configmap_round_trips(ctx: EnvContext) -> None:
    workloads = render_workloads(ctx)
    configmap = next(d for d in workloads if d["kind"] == "ConfigMap")
    assert configmap["metadata"]["name"] == "persona"
    raw = configmap["data"]["persona.yaml"]
    parsed = Persona.model_validate(yaml.safe_load(raw))
    assert parsed.name == ctx.persona.name
    assert parsed.default_ttl == ctx.persona.default_ttl
    assert parsed.max_ttl == ctx.persona.max_ttl
    assert parsed.resources == ctx.persona.resources
    assert parsed.fixtures == ctx.persona.fixtures


def test_persona_configmap_mounted_by_crewline_and_seed(ctx: EnvContext) -> None:
    deployment = next(
        d
        for d in render_workloads(ctx)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "crewline"
    )
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    persona_vol = next(v for v in volumes if v["name"] == "persona")
    assert persona_vol["configMap"]["name"] == "persona"

    job = render_seed_job(ctx)
    job_volumes = job["spec"]["template"]["spec"]["volumes"]
    job_persona_vol = next(v for v in job_volumes if v["name"] == "persona")
    assert job_persona_vol["configMap"]["name"] == "persona"


# --- Postgres --------------------------------------------------------------


def test_postgres_password_is_deterministic_for_same_owner_and_namespace(
    ctx: EnvContext,
) -> None:
    workloads_a = render_workloads(ctx)
    workloads_b = render_workloads(ctx)
    secret_a = next(d for d in workloads_a if d["kind"] == "Secret")
    secret_b = next(d for d in workloads_b if d["kind"] == "Secret")
    password_a = secret_a["stringData"]["POSTGRES_PASSWORD"]
    password_b = secret_b["stringData"]["POSTGRES_PASSWORD"]
    assert password_a == password_b


def test_postgres_password_differs_across_owners(ctx: EnvContext, persona: Persona) -> None:
    other = EnvContext(
        env_name=ctx.env_name,
        namespace=ctx.namespace,
        persona=persona,
        expires_at=ctx.expires_at,
        owner_uid="a-completely-different-uid",
        base_domain=ctx.base_domain,
    )
    secret_a = next(d for d in render_workloads(ctx) if d["kind"] == "Secret")
    secret_b = next(d for d in render_workloads(other) if d["kind"] == "Secret")
    assert (
        secret_a["stringData"]["POSTGRES_PASSWORD"] != secret_b["stringData"]["POSTGRES_PASSWORD"]
    )


def test_postgres_uses_pinned_alpine_image_and_emptydir(ctx: EnvContext) -> None:
    deployment = next(
        d
        for d in render_workloads(ctx)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "postgres"
    )
    container = _containers(deployment)[0]
    assert re.fullmatch(r"postgres:16(\.\d+)?-alpine", container["image"])
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    assert next(v for v in volumes if v["name"] == "data")["emptyDir"] == {}


def _postgres_deployment(ctx: EnvContext) -> dict[str, Any]:
    return next(
        d
        for d in render_workloads(ctx)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "postgres"
    )


def test_postgres_probes_use_tcp_pg_isready(ctx: EnvContext) -> None:
    # Over TCP, not the Unix socket: during initdb the entrypoint runs a
    # socket-only temporary server, which must not count as ready.
    expected = ["pg_isready", "-h", "127.0.0.1", "-p", "5432", "-U", "crewline", "-d", "crewline"]
    container = _containers(_postgres_deployment(ctx))[0]
    assert container["readinessProbe"]["exec"]["command"] == expected
    assert container["livenessProbe"]["exec"]["command"] == expected


def test_postgres_deployment_uses_recreate_strategy(ctx: EnvContext) -> None:
    assert _postgres_deployment(ctx)["spec"]["strategy"] == {"type": "Recreate"}


# --- Seed Job --------------------------------------------------------------


def test_seed_job_fields(ctx: EnvContext) -> None:
    job = render_seed_job(ctx)
    assert job["spec"]["backoffLimit"] == 2
    assert job["spec"]["activeDeadlineSeconds"] == 240
    assert "ttlSecondsAfterFinished" not in job["spec"]
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    container = _containers(job)[0]
    assert container["command"][:2] == ["python", "-m"]
    assert "demoapp.seed" in container["command"]
    assert "--persona-file" in container["command"]


# --- CRD (offline validation; server-side apply deferred, see report) -----


@pytest.fixture(scope="module")
def crd() -> dict[str, Any]:
    return yaml.safe_load(CRD_PATH.read_text())


def test_crd_loads(crd: dict[str, Any]) -> None:
    assert crd["kind"] == "CustomResourceDefinition"
    assert crd["apiVersion"] == "apiextensions.k8s.io/v1"
    assert crd["metadata"]["name"] == "demoenvironments.orchestrator.local"


def test_crd_group_scope_and_names(crd: dict[str, Any]) -> None:
    assert crd["spec"]["group"] == GROUP
    assert crd["spec"]["scope"] == "Cluster"
    names = crd["spec"]["names"]
    assert names["plural"] == "demoenvironments"
    assert names["kind"] == KIND
    assert set(names["shortNames"]) == {"demoenv", "de"}


def test_crd_version_and_status_subresource(crd: dict[str, Any]) -> None:
    versions = crd["spec"]["versions"]
    assert len(versions) == 1
    version = versions[0]
    assert version["name"] == VERSION
    assert version["served"] is True
    assert version["storage"] is True
    assert version["subresources"] == {"status": {}}


def test_crd_printer_columns(crd: dict[str, Any]) -> None:
    columns = {c["name"]: c for c in crd["spec"]["versions"][0]["additionalPrinterColumns"]}
    assert columns["Persona"]["jsonPath"] == ".spec.persona"
    assert columns["Phase"]["jsonPath"] == ".status.phase"
    assert columns["Expires"]["jsonPath"] == ".status.expiresAt"
    assert columns["URL"]["jsonPath"] == ".status.url"
    assert columns["Age"]["jsonPath"] == ".metadata.creationTimestamp"
    assert columns["Age"]["type"] == "date"


def test_crd_ttl_pattern_equals_global_constraints_regex(crd: dict[str, Any]) -> None:
    ttl_schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "properties"
    ]["ttl"]
    assert ttl_schema["pattern"] == TTL_REGEX
    assert ttl_schema["minLength"] == 2


def test_crd_persona_pattern_present(crd: dict[str, Any]) -> None:
    spec_props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "properties"
    ]
    assert spec_props["persona"]["pattern"] == "^[a-z][a-z0-9-]{1,20}$"
    assert crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "required"
    ] == ["persona", "ttl"]


def test_crd_status_phase_enum(crd: dict[str, Any]) -> None:
    status_props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"][
        "properties"
    ]
    assert status_props["phase"]["enum"] == [
        "Pending",
        "Provisioning",
        "Seeding",
        "Ready",
        "Expiring",
        "Failed",
    ]


def _walk_schema_properties(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield every nested schema node under a `properties` mapping (offline structural check)."""
    nodes = []
    for prop in node.get("properties", {}).values():
        nodes.append(prop)
        nodes.extend(_walk_schema_properties(prop))
    return nodes


def test_crd_schema_is_structural_as_far_as_offline_checking_allows(crd: dict[str, Any]) -> None:
    """Offline heuristic for a "structural schema" (full validation needs a live apiserver;
    see the report's Deferred Verification section per Ruling R7)."""
    root = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    assert root["type"] == "object"
    for node in [root, *_walk_schema_properties(root)]:
        assert "type" in node, f"schema node missing an explicit type: {node}"


def test_ttl_pattern_rejects_uppercase_and_bad_grammar_offline() -> None:
    """Semantic equivalent of `kubectl apply` rejecting `ttl: 2H` (Docker/kind unavailable
    per Ruling R7 — see the report's Deferred Verification section).

    `""` is deliberately not checked here: the bare pattern (all groups optional)
    matches the empty string on its own — it's the CRD's `minLength: 2` alongside
    the pattern that rejects it, checked in `test_crd_ttl_minlength_rejects_empty`.
    """
    pattern = re.compile(TTL_REGEX)
    for bad in ("2H", "1d", "90", " 2h ", "-1h"):
        assert not pattern.fullmatch(bad), bad
    for good in ("2h", "30m", "1h30m", "45s"):
        assert pattern.fullmatch(good), good


def test_crd_ttl_minlength_rejects_empty(crd: dict[str, Any]) -> None:
    ttl_schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "properties"
    ]["ttl"]
    assert re.compile(ttl_schema["pattern"]).fullmatch("")  # pattern alone allows it
    assert len("") < ttl_schema["minLength"]  # minLength is what rejects it
