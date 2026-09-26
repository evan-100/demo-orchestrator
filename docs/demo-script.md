# Demo script (2 minutes)

A shot list for recording a walkthrough. Run `make up` beforehand so the cluster,
ingress and operator are already deployed — don't burn recording time on `kind
create cluster`.

| Time | Action | What it shows |
|---|---|---|
| 0:00–0:10 | `democtl personas` | The three personas available (healthcare, manufacturing, restaurant) — data-driven, not code. |
| 0:10–0:35 | In two terminals, run at the same time:<br>`democtl create --persona healthcare --ttl 15m`<br>`democtl create --persona manufacturing --ttl 15m` | Two isolated demo environments provisioning in parallel, each printing its name, URL and phase timings once Ready (~30s). |
| 0:35–0:55 | Open both URLs side by side in a browser (`http://<name>.demo.localtest.me`) | Two fully separate, differently-branded Crewline instances — same app, different company name, color, roles and headcount. |
| 0:55–1:10 | `kubectl get de` (short name for `demoenvironments`) | The printer columns: Persona, Phase, Expires, URL, Age — the raw Kubernetes interface, not just the CLI. |
| 1:10–1:25 | `democtl extend healthcare-xxxx --by 30m` | TTL extension is a live patch to `spec.ttl`; `kubectl get de` immediately shows the new `Expires` column. |
| 1:25–1:50 | `kubectl -n demo-orchestrator scale deploy demo-orchestrator-operator --replicas=0`, then wait, then `kubectl get pods -n demo-<manufacturing-env>` | The operator is down. A minute later the sweeper CronJob's next run reaps the manufacturing environment on its own (visible in `kubectl get de` going to `Expiring`/disappearing, or `kubectl logs -n demo-orchestrator -l app.kubernetes.io/component=sweeper`) — the two-layer expiry backstop, with no operator running. Scale the operator back to 1 replica afterward. |
| 1:50–2:00 | `democtl metrics` | Provisioning p50/p95, cleanup reliability, and the cost model — computed from the ledger, not typed in by hand. |

**Cleanup after recording:** `democtl delete healthcare-xxxx --wait` for anything
still running, and confirm the operator is back at 1 replica
(`kubectl -n demo-orchestrator get deploy demo-orchestrator-operator`).
