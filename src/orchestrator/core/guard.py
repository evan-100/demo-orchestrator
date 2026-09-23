"""The deletion guard: the single source of truth for what may be deleted.

Every namespace delete in this codebase must go through
`is_deletable_namespace`. No exceptions (see global-constraints.md, spec A8).
"""

from __future__ import annotations

from collections.abc import Mapping

from orchestrator.constants import (
    LABEL_MANAGED_BY,
    MANAGED_BY_VALUE,
    NS_PREFIX,
    PROTECTED_NAMESPACES,
)


def is_deletable_namespace(name: str, labels: Mapping[str, str] | None) -> bool:
    """True only if `name` is a namespace this operator is allowed to delete.

    All three conditions are required:
    - `labels` carries `LABEL_MANAGED_BY: MANAGED_BY_VALUE` exactly (a missing
      `labels` mapping counts as no labels, i.e. not deletable)
    - `name` starts with `NS_PREFIX`
    - `name` is not in `PROTECTED_NAMESPACES`
    """
    if labels is None:
        return False
    return (
        labels.get(LABEL_MANAGED_BY) == MANAGED_BY_VALUE
        and name.startswith(NS_PREFIX)
        and name not in PROTECTED_NAMESPACES
    )
