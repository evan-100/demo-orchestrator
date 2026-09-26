from datetime import UTC, datetime, timedelta

from orchestrator.constants import (
    ANNOTATION_EXPIRES_AT,
    LABEL_ENV,
    LABEL_MANAGED_BY,
    MANAGED_BY_VALUE,
)
from orchestrator.core.sweep import NsInfo, plan_sweep

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
G = timedelta(seconds=120)


def ns(name, env, expires=None, age=timedelta(hours=1), labels=None, phase="Active", raw_ann=None):
    lab = labels if labels is not None else {LABEL_MANAGED_BY: MANAGED_BY_VALUE, LABEL_ENV: env}
    ann = (
        {}
        if expires is None and raw_ann is None
        else {ANNOTATION_EXPIRES_AT: raw_ann or expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    )
    return NsInfo(name, lab, ann, NOW - age, phase)


def kinds(actions):
    return {(a.namespace, a.kind) for a in actions}


def test_within_grace_is_left_for_operator():
    assert plan_sweep([ns("demo-a", "a", NOW - timedelta(seconds=60))], {"a"}, NOW, G) == []


def test_past_grace_is_reaped():
    assert kinds(plan_sweep([ns("demo-a", "a", NOW - timedelta(seconds=121))], {"a"}, NOW, G)) == {
        ("demo-a", "reap_expired")
    }


def test_unmanaged_and_protected_are_invisible():
    past = NOW - timedelta(hours=1)
    spoofed = ns("kube-system", "x", past)
    unlabeled = ns("demo-b", "b", past, labels={})
    assert plan_sweep([spoofed, unlabeled], set(), NOW, G) == []


def test_orphan_reaped_only_after_5_minutes():
    young = ns("demo-c", "c", NOW + timedelta(hours=1), age=timedelta(minutes=2))
    old = ns("demo-d", "d", NOW + timedelta(hours=1), age=timedelta(minutes=6))
    assert kinds(plan_sweep([young, old], set(), NOW, G)) == {("demo-d", "reap_orphan")}


def test_garbage_annotation_not_reaped_until_hard_max():
    fresh = ns("demo-e", "e", raw_ann="not-a-date", age=timedelta(hours=1))
    ancient = ns("demo-f", "f", raw_ann="not-a-date", age=timedelta(hours=8, minutes=3))
    assert kinds(plan_sweep([fresh, ancient], {"e", "f"}, NOW, G)) == {
        ("demo-f", "reap_unparseable")
    }


def test_terminating_is_skipped():
    assert kinds(
        plan_sweep(
            [ns("demo-g", "g", NOW - timedelta(hours=1), phase="Terminating")], {"g"}, NOW, G
        )
    ) == {("demo-g", "skip_terminating")}


# Beyond the brief: rule order and edge cases.


def test_guard_runs_before_everything_else():
    past = NOW - timedelta(hours=10)
    spoofed_terminating = ns("kube-system", "x", past, phase="Terminating")
    unmanaged_ancient = ns("demo-h", "h", labels={LABEL_ENV: "h"}, age=timedelta(hours=9))
    wrong_value = ns("demo-i", "i", past, labels={LABEL_MANAGED_BY: "someone-else"})
    protected = ns("demo-orchestrator", "x", past)
    assert (
        plan_sweep([spoofed_terminating, unmanaged_ancient, wrong_value, protected], set(), NOW, G)
        == []
    )


def test_exactly_at_grace_boundary_is_not_reaped():
    assert plan_sweep([ns("demo-a", "a", NOW - G)], {"a"}, NOW, G) == []


def test_expired_takes_precedence_over_orphan():
    actions = plan_sweep([ns("demo-a", "a", NOW - timedelta(hours=1))], set(), NOW, G)
    assert [(a.kind, a.env) for a in actions] == [("reap_expired", "a")]


def test_managed_namespace_without_env_label_is_an_orphan():
    no_env = ns(
        "demo-j", None, NOW + timedelta(hours=1), labels={LABEL_MANAGED_BY: MANAGED_BY_VALUE}
    )
    young = ns(
        "demo-k",
        None,
        NOW + timedelta(hours=1),
        labels={LABEL_MANAGED_BY: MANAGED_BY_VALUE},
        age=timedelta(minutes=4),
    )
    actions = plan_sweep([no_env, young], {"j", "k"}, NOW, G)
    assert [(a.namespace, a.kind, a.env) for a in actions] == [("demo-j", "reap_orphan", None)]


def test_missing_annotation_waits_for_hard_max_unless_orphaned():
    fresh = ns("demo-l", "l", age=timedelta(hours=8, minutes=1))
    ancient = ns("demo-m", "m", age=timedelta(hours=8, minutes=3))
    orphan = ns("demo-n", "n", age=timedelta(minutes=6))
    assert kinds(plan_sweep([fresh, ancient, orphan], {"l", "m"}, NOW, G)) == {
        ("demo-m", "reap_unparseable"),
        ("demo-n", "reap_orphan"),
    }


def test_naive_timestamp_counts_as_unparseable():
    naive = ns("demo-o", "o", raw_ann="2026-09-24T10:00:00", age=timedelta(hours=2))
    assert plan_sweep([naive], {"o"}, NOW, G) == []


def test_reasons_are_human_readable():
    (action,) = plan_sweep([ns("demo-a", "a", NOW - timedelta(hours=1))], {"a"}, NOW, G)
    assert action.reason == "expired at 2026-09-24T11:00:00.000Z, past the 120s grace"
