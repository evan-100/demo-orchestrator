"""The real `KubeFacade`, backed by the official `kubernetes` client.

Every write is a server-side apply with a fixed field manager, so re-applying
the same manifests after a retry or an operator restart is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from orchestrator.constants import ANNOTATION_EXPIRES_AT, GROUP, PLURAL, VERSION
from orchestrator.core.guard import is_deletable_namespace

FIELD_MANAGER = "demo-orchestrator"


@dataclass(frozen=True)
class NamespaceInfo:
    """What the operator needs to know about a namespace before deleting it."""

    name: str
    uid: str
    resource_version: str
    labels: dict[str, str] = field(default_factory=dict)
    terminating: bool = False


class NamespaceDeletionRefusedError(Exception):
    """Raised when asked to delete a namespace the deletion guard rejects."""


def load_kube_config() -> None:
    """Use the in-cluster service account when running in a pod, else ~/.kube/config."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


class KubeClient:
    """`KubeFacade` implementation over a configured `kubernetes.client.ApiClient`."""

    def __init__(self, api_client: client.ApiClient | None = None) -> None:
        self._api_client = api_client or client.ApiClient()
        self._dynamic = DynamicClient(self._api_client)
        self._core = client.CoreV1Api(self._api_client)
        self._apps = client.AppsV1Api(self._api_client)
        self._batch = client.BatchV1Api(self._api_client)
        self._custom = client.CustomObjectsApi(self._api_client)

    def apply(self, manifest: dict[str, Any]) -> None:
        resource = self._dynamic.resources.get(
            api_version=manifest["apiVersion"], kind=manifest["kind"]
        )
        self._dynamic.server_side_apply(
            resource,
            body=manifest,
            namespace=manifest["metadata"].get("namespace"),
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )

    def deployment_available(self, ns: str, name: str) -> bool:
        try:
            deployment = self._apps.read_namespaced_deployment_status(name, ns)
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        return bool(deployment.status and (deployment.status.available_replicas or 0) >= 1)

    def job_status(self, ns: str, name: str) -> Literal["running", "succeeded", "failed"]:
        try:
            job = self._batch.read_namespaced_job_status(name, ns)
        except ApiException as exc:
            if exc.status == 404:
                return "running"
            raise
        status = job.status
        if status is None:
            return "running"
        if (status.succeeded or 0) >= 1:
            return "succeeded"
        for condition in status.conditions or []:
            if condition.type == "Failed" and condition.status == "True":
                return "failed"
        return "running"

    def count_active_envs(self, exclude: str) -> int:
        envs = self._custom.list_cluster_custom_object(GROUP, VERSION, PLURAL)
        return sum(
            1
            for env in envs.get("items", [])
            if env["metadata"]["name"] != exclude
            and (env.get("status") or {}).get("phase") != "Failed"
        )

    def set_namespace_expiry(self, ns: str, expires_at: str) -> None:
        body = {"metadata": {"annotations": {ANNOTATION_EXPIRES_AT: expires_at}}}
        try:
            self._core.patch_namespace(ns, body)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def get_namespace(self, name: str) -> NamespaceInfo | None:
        try:
            ns = self._core.read_namespace(name)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        meta = ns.metadata
        return NamespaceInfo(
            name=meta.name,
            uid=meta.uid,
            resource_version=meta.resource_version,
            labels=dict(meta.labels or {}),
            terminating=meta.deletion_timestamp is not None,
        )

    def delete_namespace(self, ns: NamespaceInfo) -> None:
        """Delete exactly the namespace described by `ns`, if the guard allows it.

        The uid/resourceVersion preconditions make the API server reject the
        delete (409) if the namespace changed since it was read and checked,
        so a relabel between the check and the delete cannot slip through.
        """
        if not is_deletable_namespace(ns.name, ns.labels):
            raise NamespaceDeletionRefusedError(f"deletion guard refused namespace {ns.name!r}")
        body = client.V1DeleteOptions(
            preconditions=client.V1Preconditions(uid=ns.uid, resource_version=ns.resource_version)
        )
        try:
            self._core.delete_namespace(ns.name, body=body)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def patch_env_status(self, name: str, status: dict[str, Any]) -> None:
        """Merge-patch the DemoEnvironment's status subresource."""
        self._custom.patch_cluster_custom_object_status(
            GROUP, VERSION, PLURAL, name, {"status": status}
        )

    def delete_env(self, name: str) -> None:
        """Delete the DemoEnvironment (its finalizer then runs); absent is fine."""
        try:
            self._custom.delete_cluster_custom_object(GROUP, VERSION, PLURAL, name)
        except ApiException as exc:
            if exc.status != 404:
                raise
