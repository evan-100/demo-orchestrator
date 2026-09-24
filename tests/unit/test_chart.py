"""Tests for the Helm chart: synced copies, sweeper RBAC, and the personas projection."""

from __future__ import annotations

import filecmp
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator.constants import GROUP, PLURAL
from orchestrator.core.personas import load_personas

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts" / "demo-orchestrator"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _relative_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    ("source", "copy"),
    [
        (REPO_ROOT / "deploy" / "crd", CHART / "crds"),
        (REPO_ROOT / "personas", CHART / "personas"),
    ],
    ids=["crds", "personas"],
)
def test_chart_copies_match_sources(source: Path, copy: Path) -> None:
    files = _relative_files(source)
    assert _relative_files(copy) == files, "chart copy is stale: run `make chart-sync`"
    for rel in files:
        assert filecmp.cmp(source / rel, copy / rel, shallow=False), (
            f"{copy / rel} differs from {source / rel}: run `make chart-sync`"
        )


def _render() -> list[dict[str, Any]]:
    out = subprocess.run(
        ["helm", "template", "demo-orchestrator", str(CHART), "-n", "demo-orchestrator"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _find(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


@needs_helm
def test_sweeper_cluster_role_is_least_privilege() -> None:
    role = _find(_render(), "ClusterRole", "demo-orchestrator-sweeper")
    rules = {
        (group, resource): set(rule["verbs"])
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
    }
    # Spec A8, plus patch on demoenvironments to strip a stuck finalizer.
    assert rules == {
        ("", "namespaces"): {"list", "get", "delete"},
        (GROUP, PLURAL): {"get", "list", "delete", "patch"},
    }


@needs_helm
def test_personas_configmap_projects_to_the_loader_layout(tmp_path: Path) -> None:
    docs = _render()
    data = _find(docs, "ConfigMap", "demo-orchestrator-personas")["data"]
    pod = _find(docs, "Deployment", "demo-orchestrator-operator")["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "personas")
    for item in volume["configMap"]["items"]:
        target = tmp_path / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data[item["key"]])

    assert load_personas(tmp_path) == load_personas(REPO_ROOT / "personas")


@needs_helm
def test_operator_cluster_role_covers_status_reads() -> None:
    # KubeClient polls readiness through the status subresources, not the objects.
    role = _find(_render(), "ClusterRole", "demo-orchestrator-operator")
    granted = {
        (group, resource, verb)
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }
    assert ("apps", "deployments/status", "get") in granted
    assert ("batch", "jobs/status", "get") in granted
    assert (GROUP, f"{PLURAL}/status", "patch") in granted
