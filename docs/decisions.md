# Architecture Decision Records

Short ADRs: context, decision, consequences. These record trade-offs made while
building the orchestrator, not a full design doc — see `docs/architecture.md` for
the system as built.

---

## ADR 1: CRD + operator (kopf), not an imperative script

**Context:** the simplest way to provision a demo environment is a script that calls
`create_namespace`, `create_deployment`, etc., and a cron job that calls `delete_namespace`
when a TTL expires.

**Decision:** model a demo environment as a `DemoEnvironment` custom resource, reconciled
by a kopf-based operator with a status subresource, owner references and a finalizer.

**Consequences:** more moving parts (a CRD, RBAC, a reconciliation loop) than a script,
but the state lives in the cluster instead of a process's memory or a database. Any
component (CLI, `kubectl`, the sweeper) can read or drive the same object, and an
operator crash or restart doesn't lose track of an in-flight provision (Task 12's
chaos tests exist because this is true). The declarative model also gives extension a
free win: raising `spec.ttl` is a `kubectl patch`, not a new code path.

## ADR 2: Namespace-per-demo, not a shared pool

**Context:** a shared namespace (or a pool of pre-warmed ones) would provision faster,
since no `Namespace`, `ResourceQuota`, `NetworkPolicy` or RBAC objects need creating
per demo.

**Decision:** every demo environment gets its own namespace (`demo-<envname>`), created
fresh, with its own quota, network policy and Postgres.

**Consequences:** provisioning is slower (namespace + app + seed take ~28s p50 — see
`docs/results.md`) and multi-tenant isolation between concurrent demos is real, not
simulated: two personas can't see each other's data even under a bug, because they're
in different namespaces with a default-deny `NetworkPolicy` between them. The cost of
this choice is exactly what a warm pool (stretch goal S1) would buy back — pre-provision
N idle-but-ready namespaces and claim one on demand, at the cost of running idle
capacity. That trade-off is deliberately left as a *measured* stretch goal rather than
an opinion in this document.

## ADR 3: TTL clock starts at CR creation, not at Ready

**Context:** the TTL could be measured from `spec.ttl` either from when the
`DemoEnvironment` is created, or from when it becomes `Ready` (i.e., "N hours of
actual demo time").

**Decision:** `status.expiresAt = status.createdAt + spec.ttl`. Provisioning time
counts against the TTL.

**Consequences:** simpler and fully declarative — `expiresAt` is a pure function of
two fields, computable by the sweeper from namespace metadata alone, with no need to
know whether provisioning succeeded. The cost is that a slow provision (or a stalled
seed Job) eats into the demo's usable window; in practice provisioning is ~30s against
TTLs measured in hours, so this is negligible. A failed provision is handled separately
(A8): `expiresAt` is forced to now + 10m so the namespace stays inspectable rather than
silently consuming the requested TTL.

## ADR 4: Two-layer expiry — operator timer plus an independent sweeper

**Context:** a single expiry mechanism (the operator's own timer handler) is a single
point of failure: if the operator is down when a demo expires, nothing cleans it up
until it comes back.

**Decision:** the operator's `@kopf.timer` handler is the primary expiry path. A
separate sweeper, run as a CronJob every minute with its own RBAC and no dependency
on kopf, lists managed namespaces directly and reaps anything past its
`expires-at` annotation plus grace — independently of whether the operator is running.

**Consequences:** this is the reliability claim the whole project is built to
demonstrate: kill the operator, expired demos still get cleaned up
(`tests/integration/test_chaos_e2e.py::test_sweeper_reaps_expired_env_while_operator_is_down`).
The cost is two places that read/interpret the same annotation and must agree on
semantics (grace period, malformed-annotation handling — Review Focus #4), and the
ledger can record either the operator's `expired`/`deleted` pair or the sweeper's
`sweeper_reaped` for the same environment, which `core.metrics` has to treat as
equivalent terminal outcomes (see (f) below).

## ADR 5: JSONL ledger on a shared RWO PVC

**Context:** the operator and sweeper both need to append lifecycle events to one
audit trail that `democtl metrics` reads. Options were a database (Postgres, SQLite
on a shared volume), an external log sink, or a flat file.

**Decision:** a single append-only JSONL file (`orchestrator.core.ledger.Ledger`),
each write wrapped in `fcntl.flock`, on a `ReadWriteOnce` PVC mounted by both the
operator Deployment and the sweeper CronJob's pods.

**Consequences:** simplest possible durable store — no schema, human-readable,
`democtl metrics` is a straight fold over the file, and a malformed line (a torn
write) is skipped with a warning instead of crashing metrics (Review Focus #5).
**The known limit:** this only works because kind is single-node, so an RWO volume
can be mounted by both pods (they land on the same node). On a multi-node cluster
(including GKE), the operator and sweeper pods could be scheduled to different
nodes and one of them would fail to mount the PVC. This is recorded here rather than
worked around in the MVP; the GKE stretch goal (S3) moves the ledger to one object
per event in a GCS bucket, which has no node-affinity constraint.

---

## Recorded limits and decisions (beyond the five ADRs above)

These are deliberate, scoped trade-offs surfaced during implementation and review,
kept here so they're visible rather than silently absorbed into the code.

**(a) Deletion-guard bypass via GC after a tampered ownerReference.** The `on.delete`
handler (ADR-1) explicitly deletes the namespace through the guard, then waits for it
to be gone; the `ownerReference` from namespace to `DemoEnvironment` is kept only as a
backstop for the case where that explicit delete never ran. But if someone tampers
with a namespace so that it still carries a valid `ownerReference` to a `DemoEnvironment`
that is later deleted, Kubernetes' own garbage collector will delete that namespace
directly — bypassing `core.guard.is_deletable_namespace` entirely, since GC doesn't call
our code. This is inherent to using `ownerReferences` as a backstop at all: the backstop
and the guard protect different failure modes (operator crash vs. malicious tampering)
and can't both be enforced by the same mechanism. No mitigation is implemented in the
MVP; an admission policy that validates `ownerReferences` on write would close this.

**(b) The ledger's RWO PVC works only because operator and sweeper share a node.**
Restated from ADR-5: this is a hard requirement for single-node kind, and needs
another store (see S3) on any multi-node cluster, GKE included.

**(c) The operator's ClusterRole can create Deployments/Jobs in any namespace.** Because
`DemoEnvironment` is cluster-scoped and each reconcile creates its per-demo Deployment,
Service, Ingress and Job in a namespace the operator itself just created, its
ClusterRole grants `create`/`update`/`delete` on Deployments, Services, Ingresses and
Jobs across *all* namespaces — not scoped to `demo-*`. This is a real, design-level
privilege: a bug or a compromised operator pod could create workloads outside the
demo namespaces it's meant to own. Mitigation ideas, not implemented: a
`ValidatingAdmissionPolicy` restricting the operator's ServiceAccount to
`metadata.namespace` matching `^demo-`, or splitting per-demo resource creation into a
namespaced helper ServiceAccount created per demo (adds complexity for a
single-tenant-per-namespace MVP).

**(d) The sweeper may strip the finalizer from a `DemoEnvironment` it reaps.** Spec A8
originally implied only the operator manages the finalizer; Ruling R14 amends this —
when the sweeper reaps a namespace whose owning `DemoEnvironment` still carries the
operator's finalizer (e.g., the operator has been down long enough that it never got
to run its own delete handler), the sweeper patches the CR to remove the finalizer
after it has deleted the namespace, so the CR itself doesn't hang forever waiting for
an operator that may not come back soon.

**(e) The CRD ships in the chart's `crds/` directory, and `make deploy` applies it
server-side before `helm upgrade`.** Helm's `crds/` convention installs a CRD once and
never upgrades or deletes it on subsequent releases — by design, to avoid Helm
accidentally deleting a CRD (and every custom resource of that kind) on
`helm uninstall`. That means a schema change to the CRD would silently not take effect
on an existing install if the chart's `crds/` directory were the only mechanism. `make
deploy` works around this by running `kubectl apply --server-side --force-conflicts -f
deploy/crd/` immediately before `helm upgrade --install`, so an existing cluster always
gets the current CRD schema; the chart's `crds/` copy (kept in sync by `make
chart-sync`) exists to make `helm install` work standalone too.

**(f) Terminal ledger events are at-least-once, not exactly-once.** Because the
operator's timer and the sweeper each have their own view of an environment's state,
both can independently decide it has reached a terminal outcome and append their own
event for it (e.g., the operator writes `expired` then `deleted` while the sweeper's
next tick also observes the (now half-deleted) namespace and writes
`sweeper_reaped`). `core.metrics` treats this as expected and de-duplicates per
environment, counting only the first terminal event seen for a given `env` — whichever
of `deleted`, `delete_timeout` or `sweeper_reaped` appears earliest in the ledger —
rather than assuming one event per environment.
