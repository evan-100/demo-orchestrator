import pytest

from orchestrator.constants import LABEL_MANAGED_BY, MANAGED_BY_VALUE
from orchestrator.core.guard import is_deletable_namespace

OK = {LABEL_MANAGED_BY: MANAGED_BY_VALUE}


def test_managed_demo_namespace_is_deletable():
    assert is_deletable_namespace("demo-healthcare-7f3k", OK)


@pytest.mark.parametrize(
    "name,labels",
    [
        ("demo-healthcare-7f3k", None),  # no labels
        ("demo-healthcare-7f3k", {LABEL_MANAGED_BY: "helm"}),  # wrong manager
        ("healthcare-7f3k", OK),  # missing prefix
        ("kube-system", OK),  # protected + spoofed label
        ("default", OK),
        ("demo-orchestrator", OK),  # has prefix but protected
    ],
)
def test_everything_else_is_not(name, labels):
    assert not is_deletable_namespace(name, labels)
