# Postmortem

> **Draft.** This was reconstructed from the build log, review findings and test runs. Edit it into your own words.

## What worked well

- **Keeping state in the cluster, not the process.** The operator keeps phase checkpoints on the CR status and the expiry time as a namespace annotation. It keeps nothing important in memory. The chaos tests show why that matters:
  - Killing the operator pod mid-seed still converged to Ready, with exactly one `requested` event and one `ready` event in the ledger.
  - With the operator scaled to 0, the sweeper still reaped the expired demo using only namespace metadata.
- **Two independent expiry layers.** The operator timer handles every normal case: 10 of 10 on time, with a median lag of 13 s. The sweeper is a CronJob that doesn't depend on the operator, so "cleanup still happens when the operator is down" is something a test can check, not just a design goal.
- **One deletion guard, checked everywhere.** Every namespace delete, whether from the operator or the sweeper, goes through the same three-check function: managed-by label, `demo-` prefix, and not on the protected list. It is re-checked against a freshly fetched object, with uid and resourceVersion preconditions. No test or live run ever deleted anything it didn't own.
- **Metrics come only from the ledger.** Because the numbers in `docs/results.md` are computed from an append-only log, they can be re-run and audited rather than timed by hand.
- **Pure logic kept apart from Kubernetes I/O.** `orchestrator.core` has 94% unit coverage and runs in about 4 s. The Kubernetes calls sit behind small facade interfaces, so the operator, sweeper and CLI logic can all be tested against in-memory fakes.

## What was harder than expected

- **Finalizers and garbage collection interact in non-obvious ways.** The original plan had the delete finalizer *wait* for the owned namespace to disappear. But background garbage collection only removes dependents after the owner is fully gone, and the finalizer is exactly what keeps the owner around. That would have deadlocked, and it was caught before any code was written. The fix is for the finalizer to delete the namespace itself, keeping the ownerReference only as a backstop. Bounding the finalizer took a second fix: an error raised *before* the 120 s check could keep kopf retrying forever. The fix measures time from `deletionTimestamp` first and adds a kopf-level `timeout=150`.
- **Readiness ordering.** The plan said to wait for the app Deployment to be Ready, then seed. But the app's `/readyz` requires the seed marker, so that order deadlocks. The working order is postgres ready → seed Job → app ready.
- **At-least-once events from more than one producer.** The operator and the sweeper can both write a terminal event for the same demo when the operator is running behind. The metrics have to de-duplicate per demo (first of `deleted` / `delete_timeout` / `sweeper_reaped` wins), rather than assume exactly one.
- **Running on a laptop.** Docker Desktop has about 3.8 GiB on an 8 GB M1. That caps concurrent demos at about 2 during tests, and the kind node container was killed once (exit 137) mid-session.

## What broke, and how it was found

- **The operator could adopt a namespace it didn't create.** This was the most serious one, found in the final whole-branch review. The first namespace apply was a forced server-side apply with no existence check.
  - `democtl create --name orchestrator` targets `demo-orchestrator`, which is the chart's own install namespace.
  - The deletion guard refused to delete it, but garbage collection would have removed it through the ownerReference the operator had just stamped on.
  - The fix has two parts: admission rejects protected names, and a pre-apply ownership check fails the demo when an existing namespace lacks our label plus this CR's uid.
  - Verified live: a CR named `orchestrator` goes `Failed` with "namespace 'demo-orchestrator' is protected", and the namespace keeps its uid.
- **A transient error could skip the capacity check.** Admission counted active demos *after* persisting `createdAt`. A 429 or 5xx at that moment meant admission never re-ran, bypassing `MAX_CONCURRENT_ENVS`. The fix moves all admission reads before any status or ledger write.
- **The operator could read every Secret in the cluster.** Found in the chart review with `kubectl auth can-i list secrets -n kube-system --as=…operator`, which answered `yes`. The operator only ever server-side-applies one Secret, so it now has `create` and `patch` only.
- **`make deploy` left a stale operator running.** Rebuilding the `:dev` image didn't restart the pod, because Helm saw an identical spec. The only symptom was that behaviour didn't change. The fix templates the image ID into a pod annotation.
- **`make cluster` raced.** `kubectl wait --for=condition=ready pod` ran before the ingress controller pod existed and failed with "no matching resources found". The fix is `rollout status` on the Deployment.
- **Postgres probe race.** `pg_isready` without `-h` checks the unix socket, which reports ready during initdb's socket-only phase. That burned seed Job retries, and was fixed by probing over TCP.
- **Metrics crash on valid quantities.** `memory: 1G`, a decimal SI unit, raised an unhandled `ValueError` and took down the whole report. The fix makes parsing non-raising, with per-persona warnings.
- **Stale in-app expiry banner.** `extend` updated the CR and the namespace annotation, but not the app's `EXPIRES_AT`, so the banner contradicted `kubectl get de`.

## What I'd change about the design, now that it's built

- **The ledger store.** JSONL on a ReadWriteOnce PVC only works because the operator and sweeper share a node. It was right for proving the mechanism, but it's the first thing to replace for multi-node or GKE: object storage with one object per event, or a small append API.
- **The operator's RBAC.** A ClusterRole that can create Deployments and Jobs in any namespace is the largest remaining privilege. An admission policy that limits it to `demo-*` namespaces would close most of that.
- **Namespace per demo vs. a shared pool.** Isolation made cleanup and seeding simple to reason about, and 28 s median provisioning is acceptable for a demo. About half of that (seed ~13.6 s, app ~14.4 s) is exactly what a pre-warmed pool would remove.

## What I'd do next

1. **Warm pool (S1).** It turns the isolation-vs-pool trade-off into a measured cold-vs-warm number, and the phase timings already show where the time goes.
2. **Move the ledger off the RWO PVC**, which unblocks GKE (S3) and real cost measurement against `pricing.yaml`.
3. **Prometheus metrics (S4)** for provisioning time, active demos and cleanup lag, so reliability is visible continuously rather than per bench run.

## Numbers worth remembering

- Provisioning p50 **28.0 s** / p95 **35.8 s** (n=10, 0 failed).
- Cleanup **10/10 on time**, lag p50 **13.2 s** / p95 **14.9 s**.
- With the operator scaled to 0, the sweeper still reaped an expired demo, **186 s** after expiry (120 s grace + CronJob cadence + 30 s finalizer wait).
