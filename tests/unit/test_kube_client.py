"""Unit tests for the safety-critical parts of `KubeClient`, against a mocked CoreV1Api."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from orchestrator.constants import LABEL_MANAGED_BY, MANAGED_BY_VALUE
from orchestrator.k8s.client import KubeClient, NamespaceDeletionRefusedError, NamespaceInfo

MANAGED = {LABEL_MANAGED_BY: MANAGED_BY_VALUE}


def _client(core: MagicMock) -> KubeClient:
    """A KubeClient wired to `core` only (no API server, no discovery)."""
    kube = KubeClient.__new__(KubeClient)
    kube._core = core
    return kube


def _ns(name: str, labels: dict[str, str] | None) -> NamespaceInfo:
    return NamespaceInfo(name=name, uid="uid-1", resource_version="42", labels=labels or {})


@pytest.mark.parametrize(
    ("name", "labels"),
    [
        pytest.param("demo-imposter", {}, id="demo-prefix-without-label"),
        pytest.param("kube-system", MANAGED, id="spoofed-label-outside-prefix"),
        pytest.param("demo-orchestrator", MANAGED, id="protected-install-namespace"),
    ],
)
def test_delete_namespace_refuses_what_the_guard_rejects(name: str, labels: dict[str, str]) -> None:
    core = MagicMock()
    with pytest.raises(NamespaceDeletionRefusedError):
        _client(core).delete_namespace(_ns(name, labels))
    core.delete_namespace.assert_not_called()


def test_delete_namespace_sends_uid_and_resource_version_preconditions() -> None:
    core = MagicMock()
    _client(core).delete_namespace(_ns("demo-healthcare-ab12", MANAGED))
    core.delete_namespace.assert_called_once()
    args, kwargs = core.delete_namespace.call_args
    assert args == ("demo-healthcare-ab12",)
    body = kwargs["body"]
    assert isinstance(body, client.V1DeleteOptions)
    assert body.preconditions.uid == "uid-1"
    assert body.preconditions.resource_version == "42"


def test_delete_namespace_treats_404_as_done_and_raises_on_conflict() -> None:
    core = MagicMock()
    core.delete_namespace.side_effect = ApiException(status=404)
    _client(core).delete_namespace(_ns("demo-healthcare-ab12", MANAGED))

    core.delete_namespace.side_effect = ApiException(status=409)  # precondition failed
    with pytest.raises(ApiException):
        _client(core).delete_namespace(_ns("demo-healthcare-ab12", MANAGED))


def test_get_namespace_reports_owner_uids() -> None:
    core = MagicMock()
    core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(
            name="demo-x",
            uid="uid-ns",
            resource_version="3",
            labels=dict(MANAGED),
            deletion_timestamp=None,
            owner_references=[SimpleNamespace(uid="cr-uid")],
        )
    )
    ns = _client(core).get_namespace("demo-x")
    assert ns == NamespaceInfo(
        name="demo-x", uid="uid-ns", resource_version="3", labels=MANAGED, owner_uids=("cr-uid",)
    )


def test_get_namespace_without_owner_references_has_no_owner_uids() -> None:
    core = MagicMock()
    core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(
            name="demo-x",
            uid="uid-ns",
            resource_version="3",
            labels=None,
            deletion_timestamp=None,
            owner_references=None,
        )
    )
    ns = _client(core).get_namespace("demo-x")
    assert ns is not None and ns.owner_uids == () and ns.labels == {}
