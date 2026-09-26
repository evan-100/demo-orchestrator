# Results

These numbers come from a real `democtl bench` run against the in-cluster operator
on a local kind cluster, computed from the JSONL ledger (`core.metrics`). Nothing
here is hand-typed or estimated — see [Reproducing](#reproducing) to regenerate it.

## Run

- **Command:** `uv run democtl bench --persona healthcare --n 10 --ttl 2m --from-cluster`
- **Date:** 2026-09-26 (21:34:53Z – 21:41:44Z, ~6m50s wall clock)
- **Commit:** `f9ffa89`
- **Environment:**

| | |
|---|---|
| CPU | Apple M1, 8 cores |
| Memory | 8 GiB (Docker Desktop: ≈3.8 GiB (4,106,604,544 bytes) / 8 CPUs allotted) |
| macOS | 26.2 |
| Docker | 29.8.0 |
| kind | v0.33.0 (go1.27.0 darwin/arm64) |
| Kubernetes | v1.37.0 |
| Helm | v4.3.0+gbec5b06 |

## Provisioning

| N | P50 (s) | P95 (s) | Max (s) | Failed | appReadySeconds | namespaceSeconds | seedSeconds | totalSeconds |
|---|---|---|---|---|---|---|---|---|
| 10 | 28.0 | 35.8 | 35.8 | 0 | 14.4 | 0.4 | 13.6 | 28.4 |

## Cleanup

| Expired | On time | Reliability | Lag P50 (s) | Lag P95 (s) | Reaped by sweeper | Delete timeouts | In flight |
|---|---|---|---|---|---|---|---|
| 10 | 10 | 100.0% | 13.2 | 14.9 | 0 | 0 | 0 |

All 10 environments were torn down within the 120s grace window of their computed
`expiresAt`; none needed the sweeper backstop (the operator's own timer handled every
one). The sweeper's independent reap path is exercised separately by the chaos e2e
test below, not by this bench run.

## Cost

| On-demand | Baseline | Savings | Window (h) | Pricing source | Retrieved |
|---|---|---|---|---|---|
| $0.02 | $0.02 | -11.6% | 0.1 | https://cloud.google.com/kubernetes-engine/pricing | 2026-09-26 |

**Why this run shows a *negative* saving.** `core.metrics` compares the on-demand cost
of the 10 created environments against a baseline of one always-on environment for the
same wall-clock window (per persona). This bench run intentionally created 10
`healthcare` environments back-to-back with a 2-minute TTL, so multiple of them
overlapped inside a roughly 6-minute window. Ten overlapping quota-sized environments
cost more, for that short window, than one always-on environment would. **This is a
property of the benchmark's stress shape, not of the on-demand model** — it exists to
measure provisioning and cleanup speed under load, not to demonstrate cost savings.
Real usage (a handful of demos per day, not ten queued in six minutes) looks like the
modelled scenario below.

**Measured per-environment cost rate** (all three personas request the same quota:
1 vCPU / 1 GiB):

```
$0.0445/vCPU-h + $0.0049225/GiB-h  ×  1 vCPU, 1 GiB quota
= $0.0494225 per environment-hour   (us-central1, on-demand, from pricing.yaml)
```

### Modelled scenario (not measured)

Illustrative only — computed from `pricing.yaml`'s rate above, with explicit
assumptions, not from a bench run:

- **Baseline:** 3 personas, one environment per persona, always on, 24h/day.
- **On-demand:** 10 demos/day, 2h TTL each (the persona default), same quota.

| | Envs | Hours/day | $/env-hour | Cost/day |
|---|---|---|---|---|
| Always-on baseline | 3 | 24 | $0.0494225 | $3.56 |
| On-demand (modelled) | 10 | 2 | $0.0494225 | $0.99 |

Modelled savings: **72.2%** ($3.56 → $0.99/day). This is a scenario, not a
measurement — real savings depend on how many demos are actually requested per day.

## Chaos / resilience

The chaos e2e suite (`pytest -m "integration and chaos"`, run on `workflow_dispatch`
only — see `.github/workflows/ci.yml`) drives the deployed operator and sweeper
directly:

- **Sweeper backstop:** operator scaled to 0 replicas → an expired environment is
  reaped by the sweeper CronJob alone, with no operator running.
- **Restart mid-provision:** operator killed while an environment is still
  provisioning → a fresh operator pod converges it to `Ready` from cluster state.
- **Capacity limit:** environments beyond `MAX_CONCURRENT_ENVS` go to `Failed` with
  a `capacity: N/N` message.

See `tests/integration/test_chaos_e2e.py` for the exact assertions.

<details>
<summary>Raw bench log (per-environment lines)</summary>

```
START 2026-09-26T21:34:53Z
bench tag=bench-20260926T213454Z persona=healthcare n=10 ttl=2m parallel=1
healthcare-738h: created
healthcare-738h: Ready (30.7s)
healthcare-mvoo: created
healthcare-mvoo: Ready (26.2s)
healthcare-kmvx: created
healthcare-kmvx: Ready (30.4s)
healthcare-if08: created
healthcare-if08: Ready (30.4s)
healthcare-9c49: created
healthcare-9c49: Ready (26.3s)
healthcare-m7zu: created
healthcare-m7zu: Ready (30.5s)
healthcare-exlj: created
healthcare-exlj: Ready (37.0s)
healthcare-l33p: created
healthcare-l33p: Ready (26.3s)
healthcare-ugxq: created
healthcare-ugxq: Ready (26.3s)
healthcare-yxdp: created
healthcare-yxdp: Ready (36.6s)
healthcare-9c49: terminal event seen
healthcare-mvoo: terminal event seen
healthcare-kmvx: terminal event seen
healthcare-if08: terminal event seen
healthcare-738h: terminal event seen
healthcare-m7zu: terminal event seen
healthcare-exlj: terminal event seen
healthcare-l33p: terminal event seen
healthcare-ugxq: terminal event seen
healthcare-yxdp: terminal event seen
bench bench-20260926T213454Z done: ready=10 failed=0 n=10
exit=0
END 2026-09-26T21:41:44Z
```

</details>

## Reproducing

```bash
make up                                                          # kind + ingress + build + load + deploy
uv run democtl bench --persona healthcare --n 10 --ttl 2m --from-cluster
```

`make bench` runs the same command.
