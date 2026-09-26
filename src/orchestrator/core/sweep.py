"""Sweep planning: which demo namespaces the sweeper should reap, and why.

Pure logic, no Kubernetes I/O. The sweeper process (`orchestrator.sweeper.main`)
lists namespaces and DemoEnvironments, calls `plan_sweep`, and executes the
result. The sweeper is the backstop for when the operator is down or lagging,
so it trusts only namespace metadata (labels, the expires-at annotation,
creation time and phase), never the operator.

Rules, in order, per namespace (the first that matches wins):

1. Not `is_deletable_namespace` -> ignored silently, not even an action.
2. `Terminating` -> `skip_terminating` (already on its way out).
3. The expires-at annotation parses and `now > expires_at + grace` -> `reap_expired`.
4. The env label names no existing DemoEnvironment and the namespace is older
   than `ORPHAN_MIN_AGE` -> `reap_orphan`. The age floor covers the window in
   which the operator has created the namespace but the CR list we were given
   predates it.
5. The annotation is missing or unparseable -> `reap_unparseable`, but only once
   the namespace is older than the 8h hard TTL ceiling plus grace; before that it
   is left alone. A corrupted annotation must never cause an instant reap.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from orchestrator.constants import ANNOTATION_EXPIRES_AT, HARD_MAX_TTL_SECONDS, LABEL_ENV
from orchestrator.core.expiry import from_rfc3339, to_rfc3339
from orchestrator.core.guard import is_deletable_namespace

ORPHAN_MIN_AGE = timedelta(minutes=5)
HARD_MAX_AGE = timedelta(seconds=HARD_MAX_TTL_SECONDS)
TERMINATING = "Terminating"

SweepKind = Literal["reap_expired", "reap_orphan", "reap_unparseable", "skip_terminating"]


@dataclass(frozen=True)
class NsInfo:
    """The namespace metadata the sweep plan is based on.

    `phase` is the namespace's `status.phase` ("Active" or "Terminating").
    `deletion_started_at` is its `metadata.deletionTimestamp`, if any; the plan
    ignores it, the sweeper uses it to log each skipped deletion only once.
    """

    name: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    created_at: datetime
    phase: str
    deletion_started_at: datetime | None = None


@dataclass(frozen=True)
class SweepAction:
    namespace: str
    env: str | None
    kind: SweepKind
    reason: str


def env_of(ns: NsInfo) -> str | None:
    """The env name from the namespace's env label (None if missing or empty)."""
    return ns.labels.get(LABEL_ENV) or None


def parse_expires_at(ns: NsInfo) -> datetime | None:
    """The namespace's expires-at annotation as a UTC datetime, or None if missing/unparseable."""
    raw = ns.annotations.get(ANNOTATION_EXPIRES_AT)
    if raw is None:
        return None
    try:
        return from_rfc3339(raw)
    except ValueError:
        return None


def _plan_one(
    ns: NsInfo, existing_envs: set[str], now: datetime, grace: timedelta
) -> SweepAction | None:
    if not is_deletable_namespace(ns.name, ns.labels):
        return None
    env = env_of(ns)

    def action(kind: SweepKind, reason: str) -> SweepAction:
        return SweepAction(namespace=ns.name, env=env, kind=kind, reason=reason)

    if ns.phase == TERMINATING:
        return action("skip_terminating", "namespace is already terminating")

    expires_at = parse_expires_at(ns)
    if expires_at is not None and now > expires_at + grace:
        return action(
            "reap_expired",
            f"expired at {to_rfc3339(expires_at)}, past the {int(grace.total_seconds())}s grace",
        )

    age = now - ns.created_at
    if env not in existing_envs and age > ORPHAN_MIN_AGE:
        what = f"DemoEnvironment {env!r} does not exist" if env else "no env label"
        return action("reap_orphan", f"{what} (namespace age {int(age.total_seconds())}s)")

    if expires_at is None and age > HARD_MAX_AGE + grace:
        state = "unparseable" if ANNOTATION_EXPIRES_AT in ns.annotations else "missing"
        return action(
            "reap_unparseable",
            f"expires-at annotation {state} and namespace age {int(age.total_seconds())}s "
            f"exceeds the {HARD_MAX_TTL_SECONDS}s hard maximum plus grace",
        )
    return None


def plan_sweep(
    namespaces: Iterable[NsInfo], existing_envs: set[str], now: datetime, grace: timedelta
) -> list[SweepAction]:
    """Decide, per namespace, whether the sweeper reaps it, skips it, or leaves it alone.

    `existing_envs` is the set of DemoEnvironment names currently in the cluster.
    Namespaces that need nothing (and all namespaces the guard rejects) produce
    no action. See the module docstring for the rules.
    """
    actions: list[SweepAction] = []
    for ns in namespaces:
        planned = _plan_one(ns, existing_envs, now, grace)
        if planned is not None:
            actions.append(planned)
    return actions
