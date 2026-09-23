# Demo Environment Orchestrator — Project Spec & Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `AGENTS.md` first for conventions. Part A is the spec (the *what* and *why*); Part B is the plan (the *how*). When they disagree, Part A wins. Stop and ask the human.

**Goal:** A Kubernetes operator, written in Python, that provisions isolated, persona-seeded demo environments on demand and reliably tears them down when their TTL expires, with every lifecycle event recorded in an audit ledger that the headline metrics are computed from.

**Architecture:** A `DemoEnvironment` custom resource is the API. A kopf-based operator reconciles each one into a dedicated namespace. The namespace gets quotas, a network policy, a sample SaaS app, Postgres, and a persona-specific seed Job. The operator expires environments on a timer. A separate sweeper CronJob acts as an independent safety net: it reaps anything the operator missed, even while the operator is down. Both append to a shared JSONL ledger, and `democtl metrics` computes provisioning time, cleanup reliability and cost from it.

**Tech Stack:** Python 3.12, uv, kopf, kubernetes (official Python client), Typer + Rich (CLI), FastAPI + Jinja2 + SQLAlchemy + psycopg (demo app), Pydantic v2, pytest, ruff, mypy, Docker, kind, ingress-nginx, Helm 3, GitHub Actions.

---

# PART A — SPEC

## A1. What changed from the original brief (and why)

The brief's core idea is kept: namespace per demo, TTL stored outside the process, a sweeper, and a JSONL ledger. These upgrades turn it into a stronger Kubernetes artifact:

| Brief | This spec | Why |
|---|---|---|
| A Python script calls the K8s API | **CRD + operator (kopf)** | Reconciliation loops, CRDs, status subresources, owner references and finalizers *are* the Kubernetes skill set. A script that calls `create_namespace` doesn't show them. |
| One sweeper does all expiry | **Two layers:** an operator timer (primary) plus an independent sweeper CronJob (backstop) | This produces a testable reliability claim: kill the operator and expired demos still get cleaned up. That's the chaos test in Task 12. |
| TTL label | TTL in `spec.ttl`, computed `status.expiresAt`, mirrored to a namespace annotation | Declarative: to extend a demo you just edit `spec.ttl`. The sweeper needs only namespace metadata, not the operator. |
| Helm overlay per persona | **Personas are data** (`personas/<name>/persona.yaml`). Per-demo manifests are rendered by the operator. Helm installs the orchestrator itself. | Adding a vertical becomes a new folder, not new code. Helm is still used where it fits best. |
| "Seeded demo content" (unspecified) | A concrete sample app, **Crewline**: a small workforce/HR SaaS (people, locations, shifts, certifications, payroll summary), rebranded and reseeded per persona | You need something worth *showing* in a demo video. HR/workforce data is easy to grasp at a glance. |
| Metrics measured by hand | **Metrics derived from the ledger** (`democtl metrics`, `democtl bench`) | The numbers are reproducible and auditable, so they hold up to scrutiny. |
| Shared pool only mentioned in the postmortem | **Warm pool as a stretch goal** (S1) that *measures* cold vs warm provisioning | The trade-off becomes a measured number, not an opinion. |

**Public framing:** *"an operator for ephemeral, industry-specific demo environments."*

## A2. User-facing behaviour

```bash
democtl personas                                   # list available personas
democtl create --persona healthcare --ttl 2h       # → prints name, URL, expiry; waits for Ready
democtl list                                       # table: name, persona, phase, age, expires-in, URL
democtl get healthcare-7f3k                        # full status incl. phase timings
democtl extend healthcare-7f3k --by 30m            # capped at persona max_ttl
democtl delete healthcare-7f3k                     # immediate teardown
democtl metrics [--since 7d] [--json]              # provisioning p50/p95, cleanup reliability, cost delta
democtl bench --persona healthcare --n 20 --ttl 2m # runs N create→ready→expire cycles, then prints metrics
```

Equivalent raw Kubernetes interface (a first-class feature, shown in the README):

```yaml
apiVersion: orchestrator.local/v1alpha1
kind: DemoEnvironment
metadata:
  name: healthcare-7f3k
spec:
  persona: healthcare
  ttl: 2h
  requestedBy: evan        # optional, free text, recorded in ledger
```

A Ready environment is reachable at `http://<name>.demo.localtest.me` (`*.localtest.me` resolves to 127.0.0.1; kind maps host ports 80/443 to ingress-nginx).

## A3. Resource model

**CRD:** `demoenvironments.orchestrator.local`, cluster-scoped, version `v1alpha1`, short names `demoenv`, `de`. Status subresource enabled. Printer columns: Persona, Phase, Expires, URL, Age.

**Spec (OpenAPI-validated):**
- `persona`: string, required, pattern `^[a-z][a-z0-9-]{1,20}$` (whether the persona exists is checked by the operator)
- `ttl`: string, required, pattern `^([0-9]+h)?([0-9]+m)?([0-9]+s)?$`, minLength 2 (semantic validation is done by the operator)
- `requestedBy`: string, optional, maxLength 64

**Status** (written only by the operator):
- `phase`: `Pending | Provisioning | Seeding | Ready | Expiring | Failed`
- `namespace`, `url`, `message`
- `createdAt`, `readyAt`, `expiresAt`: RFC3339 UTC with a `Z` suffix
- `timings`: `{namespaceSeconds, appReadySeconds, seedSeconds, totalSeconds}`

**TTL semantics:** the TTL clock starts at **CR creation**, not when the environment becomes Ready. So `expiresAt = createdAt + ttl`. Extending means raising `spec.ttl`. The total TTL must stay ≤ the persona's `max_ttl`, and the global hard ceiling is `8h`.

**Per-demo namespace** `demo-<envname>`:
- Labels: `app.kubernetes.io/managed-by=demo-orchestrator`, `orchestrator.local/env=<envname>`, `orchestrator.local/persona=<persona>`
- Annotation: `orchestrator.local/expires-at=<RFC3339 UTC>` (the sweeper's source of truth)
- `ownerReferences` → the `DemoEnvironment` (cluster-scoped owner of a cluster-scoped dependent is valid). Deleting the CR garbage-collects the namespace.
- Contents: `ResourceQuota` and `LimitRange` (from the persona's `resources`), a default-deny `NetworkPolicy` allowing only ingress-nginx → app and app → postgres, a `postgres` Deployment + Service (`postgres:16-alpine`, emptyDir, ephemeral by design), a `crewline` Deployment + Service + Ingress, a `persona` ConfigMap, and a `seed` Job.

## A4. Components

1. **`orchestrator.core`**: pure logic with no Kubernetes I/O. Covers durations, personas, naming, expiry maths, the deletion guard, the ledger, metrics, and sweep planning. Nearly all unit tests live here.
2. **`orchestrator.operator`**: kopf handlers. Create → provision; field change on `spec.ttl` → re-compute expiry; timer → expire; delete (with finalizer) → wait for the namespace to be gone, then write to the ledger.
3. **`orchestrator.sweeper`**: a one-shot process run by a CronJob every minute. It lists managed namespaces, calls `core.sweep.plan_sweep`, executes the resulting actions and writes to the ledger. It never imports kopf.
4. **`orchestrator.cli`**: `democtl` (Typer). Works through the Kubernetes API with the user's kubeconfig. It creates and patches CRs and reads the ledger (from a local path, or `kubectl cp` from the orchestrator pod via `--from-cluster`).
5. **`demoapp` (Crewline)**: a FastAPI app with server-rendered pages: Dashboard, People, Locations, Shifts, Certifications (with expiring-soon warnings), Payroll summary. `demoapp.seed` generates deterministic fixtures from persona config (Faker with a fixed seed). `/healthz` = process up; `/readyz` = DB reachable **and** seed marker row present.
6. **Helm chart `charts/demo-orchestrator`**: CRD, operator Deployment (1 replica), sweeper CronJob, least-privilege RBAC (separate ServiceAccounts for operator and sweeper), ledger PVC, persona ConfigMap built from `personas/`.

## A5. Personas (MVP ships three)

`personas/<name>/persona.yaml`:

```yaml
name: healthcare
display_name: Healthcare — Riverbend Clinics
brand: { company_name: Riverbend Clinics, primary_color: "#0E7C86" }
default_ttl: 2h
max_ttl: 8h
resources: { cpu: "1", memory: 1Gi, pods: 10 }
fixtures:
  seed: 4201
  locations: 3
  employees: 60
  roles: [Registered Nurse, Medical Assistant, Front Desk, Physician, Lab Tech]
  certifications: [BLS, ACLS, HIPAA Training, RN License]
  shift_pattern: 12h        # 12h | 8h | split
```

Ship **healthcare**, **manufacturing** (Northgate Fabrication; roles like Machine Operator, QA Inspector, Forklift Operator; certs OSHA-10, Forklift, Lockout/Tagout; 8h shifts) and **restaurant** (Saltbox Kitchen Group; roles like Server, Line Cook, Host, GM; certs Food Handler, Alcohol Service; split shifts). All brands are fictional. Never use real company names or logos.

## A6. Ledger

JSONL at `$LEDGER_PATH` (default `./data/ledger.jsonl` locally, `/var/lib/orchestrator/ledger.jsonl` in-cluster on the shared PVC). It is append-only: one JSON object per line, written with `O_APPEND` under an exclusive `fcntl.flock`. Operator and sweeper pods mount the same `ReadWriteOnce` PVC. That works on single-node kind because RWO is per-node. Record this in `docs/decisions.md` as a known limit for multi-node or GKE (the S3 stretch moves the ledger to object storage).

```json
{"ts":"2026-09-24T15:02:11.482Z","event":"ready","env":"healthcare-7f3k","namespace":"demo-healthcare-7f3k","persona":"healthcare","actor":"operator","details":{"provisioning_seconds":41.7,"timings":{"namespaceSeconds":0.4,"appReadySeconds":28.9,"seedSeconds":12.4,"totalSeconds":41.7}}}
```

Event types: `requested`, `ready`, `extended`, `expired`, `deleted`, `delete_timeout`, `failed`, `sweeper_reaped`, `sweeper_skipped`.

## A7. Metrics (the headline results)

Everything is computed from the ledger by `core.metrics`:
- **Provisioning time**: from `requested.ts` to `ready.ts`. Report n, p50, p95 and max, plus the average phase breakdown.
- **Cleanup reliability**: an environment counts as *on time* if its `deleted` event (namespace fully gone) comes ≤ `grace` (default 120 s) after `expiresAt`. Reliability = on_time / expired. Also report cleanup lag p50/p95 and the count reaped by the sweeper rather than the operator. Target: 100%.
- **Cost delta**: on-demand cost = Σ (quota CPU × lifetime hours × $/vCPU-hr + quota GiB × lifetime hours × $/GiB-hr). Baseline = one always-on environment per persona for the same window. Prices come from `pricing.yaml`. Its defaults must be GKE Autopilot list prices **looked up by the agent at build time, with the source URL and retrieval date in the file**. Never make up prices.
- **Warm vs cold** (only after S1): provisioning p50/p95 for each mode.

## A8. Safety & guardrails (non-negotiable)

- **Deletion guard:** code may only delete a namespace that (a) has the label `app.kubernetes.io/managed-by=demo-orchestrator`, (b) has a name starting with `demo-`, and (c) is not in `PROTECTED_NAMESPACES` (`default`, `kube-system`, `kube-public`, `kube-node-lease`, `ingress-nginx`, `demo-orchestrator`, `local-path-storage`). All three checks are required and live in `core.guard.is_deletable_namespace`. Every delete call site goes through it.
- **Limits:** `MAX_CONCURRENT_ENVS` defaults to 5 (env var). Over the limit, the CR goes to `Failed` with message `capacity: N/N environments in use`. The TTL hard ceiling is 8h.
- **Failures:** if a seed Job fails, or provisioning exceeds `PROVISION_TIMEOUT` (default 300 s), the CR goes to `Failed`. `expiresAt` is set to now + 10m so the namespace stays inspectable, and then normal expiry cleans it up.
- **State lives in the cluster.** The operator keeps nothing in memory that matters. A restart at any point must still converge (tested in Task 12).
- **RBAC:** the sweeper ServiceAccount may only `list/get/delete namespaces` and `get/list/delete demoenvironments`, plus nothing else.

## A9. Out of scope (YAGNI)

Auth/multi-tenancy, a validating admission webhook, a persistent demo database, multi-cluster, cost dashboards and UI beyond the S2 portal, and generating persona data with an LLM.

## A10. Deliverables (definition of done)

- A public repo with a README: 60-second pitch, architecture diagram (Mermaid), quickstart (`make up && democtl create --persona healthcare --ttl 30m`), and a metrics table with real numbers from `democtl bench`.
- `docs/decisions.md` (ADR-style: CRD+operator vs script, namespace-per-demo vs pool, TTL from creation, two-layer expiry, JSONL ledger limits).
- `docs/postmortem.md` (written by the human after real runs; agents create the skeleton only).
- A 2-minute walkthrough script at `docs/demo-script.md`.
- Green CI: lint, types, unit tests, and kind e2e.

---

# PART B — IMPLEMENTATION PLAN

## Global Constraints

- Python `>=3.12`. Dependencies are managed with `uv`. Pin exact versions in `uv.lock`, and pin versions current **at build time**. Don't copy versions from memory without checking PyPI.
- Layout: `src/orchestrator/`, `src/demoapp/`, `tests/unit/`, `tests/integration/`, `personas/`, `charts/demo-orchestrator/`, `deploy/kind/`, `docs/`.
- API group `orchestrator.local`, version `v1alpha1`, kind `DemoEnvironment`, plural `demoenvironments`.
- Namespace prefix `demo-`. Label key constants live **only** in `src/orchestrator/constants.py`. Import them, never retype them.
- All datetimes are timezone-aware UTC. Serialize as RFC3339 with a `Z` suffix and millisecond precision. Naive datetimes are a bug.
- Duration grammar: `^([0-9]+h)?([0-9]+m)?([0-9]+s)?$`, lowercase only, total > 0, total ≤ 8h.
- `ruff check`, `ruff format --check`, `mypy src` (strict for `orchestrator.core`) and `pytest tests/unit` must pass before every commit.
- Integration tests are marked `@pytest.mark.integration`. They require a running kind cluster and are skipped by default (`pytest -m integration` to run).
- Images: `demo-orchestrator:dev` and `crewline:dev`, built locally and loaded with `kind load docker-image`. No registry is needed for the MVP.
- Conventional Commits.

## Review Focus

1. **Deleting something it doesn't own.** A namespace named `demo-x` without the managed-by label, or `kube-system` *with* a spoofed label, must never be deleted. → Tests in Task 3 (guard) and Task 10 (sweep plan).
2. **Operator restart or outage while environments are live.** Expiry must still happen, driven by CR status and namespace annotations, never by in-memory timers. → Task 12 chaos e2e (operator scaled to 0 → sweeper reaps; operator restarted mid-provision → converges to Ready).
3. **Bad TTL input.** `0m`, `-1h`, `2H`, `1d`, `90`, `" 2h "`, `999h` and `""` must fail with a message saying what *is* accepted, not a traceback. → Task 2 tests.
4. **Clock and annotation corruption.** A missing or unparseable `expires-at` annotation must not crash the sweeper or cause an instant reap. Such a namespace is reaped only when its age > 8h + grace. → Task 10 tests.
5. **Ledger integrity.** Concurrent appends from two processes must never interleave lines. A malformed line (e.g. a truncated write) must be skipped with a warning, never crash `metrics`. → Task 4 tests.

## File Structure

```
pyproject.toml, uv.lock, Makefile, .gitignore, .dockerignore, README.md, AGENTS.md, CLAUDE.md
deploy/kind/cluster.yaml                 # 1 node, extraPortMappings 80/443, ingress-ready label
deploy/crd/demoenvironments.yaml         # CRD (also copied into chart templates by `make chart-sync`)
charts/demo-orchestrator/                # Chart.yaml, values.yaml, templates/{operator,sweeper,rbac,pvc,personas-cm,crd}.yaml
personas/{healthcare,manufacturing,restaurant}/persona.yaml
pricing.yaml
src/orchestrator/
  constants.py                           # GROUP, VERSION, PLURAL, labels, annotations, PROTECTED_NAMESPACES, limits
  config.py                              # Settings (pydantic-settings): LEDGER_PATH, PERSONAS_DIR, MAX_CONCURRENT_ENVS, PROVISION_TIMEOUT, SWEEP_GRACE, BASE_DOMAIN
  core/durations.py  core/personas.py  core/naming.py  core/expiry.py
  core/guard.py      core/ledger.py    core/metrics.py core/sweep.py  core/pricing.py
  k8s/client.py                          # thin wrappers: load config (in-cluster or kubeconfig), CR get/patch, namespace ops
  k8s/manifests.py                       # render(persona, env) -> list[dict]; templates in k8s/templates/*.yaml.j2
  operator/handlers.py                   # kopf handlers
  operator/provision.py                  # step functions used by handlers
  sweeper/main.py                        # python -m orchestrator.sweeper.main
  cli/main.py                            # democtl entrypoint
  cli/bench.py
src/demoapp/
  app.py  db.py  models.py  seed.py  fixtures.py  templates/*.html  static/app.css
docker/orchestrator.Dockerfile  docker/crewline.Dockerfile
tests/unit/...  tests/integration/...
docs/decisions.md  docs/postmortem.md  docs/demo-script.md  docs/architecture.md
.github/workflows/ci.yml
```

---

### Task 1: Scaffolding, toolchain and local cluster

**Files:** Create `pyproject.toml`, `Makefile`, `.gitignore` (append; keep the existing brief entry), `.dockerignore`, `deploy/kind/cluster.yaml`, `src/orchestrator/__init__.py`, `src/orchestrator/constants.py`, `src/orchestrator/config.py`, `tests/unit/test_smoke.py`. (`AGENTS.md` and `CLAUDE.md` already exist.)

**Interfaces — Produces:**
```python
# constants.py
GROUP = "orchestrator.local"; VERSION = "v1alpha1"; PLURAL = "demoenvironments"; KIND = "DemoEnvironment"
NS_PREFIX = "demo-"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"; MANAGED_BY_VALUE = "demo-orchestrator"
LABEL_ENV = f"{GROUP}/env"; LABEL_PERSONA = f"{GROUP}/persona"
ANNOTATION_EXPIRES_AT = f"{GROUP}/expires-at"
PROTECTED_NAMESPACES = frozenset({"default","kube-system","kube-public","kube-node-lease","ingress-nginx","demo-orchestrator","local-path-storage"})
HARD_MAX_TTL_SECONDS = 8 * 3600
# config.py
class Settings(BaseSettings): ledger_path: Path; personas_dir: Path; max_concurrent_envs: int = 5; provision_timeout: int = 300; sweep_grace: int = 120; base_domain: str = "demo.localtest.me"
def get_settings() -> Settings  # lru_cached
```

- [ ] **Step 1:** `uv init --package`, set the name to `demo-orchestrator` and `requires-python = ">=3.12"`, add runtime deps (kopf, kubernetes, typer, rich, pydantic, pydantic-settings, pyyaml, jinja2, fastapi, uvicorn, sqlalchemy, psycopg[binary], faker) and dev deps (pytest, pytest-cov, ruff, mypy, types-PyYAML, httpx). Script entrypoint: `democtl = "orchestrator.cli.main:app"`.
- [ ] **Step 2:** Write `deploy/kind/cluster.yaml`: one control-plane node, `node-labels: "ingress-ready=true"`, `extraPortMappings` for 80→80 and 443→443.
- [ ] **Step 3:** Makefile targets: `cluster` (kind create + install ingress-nginx kind manifest + wait), `down`, `build`, `load`, `deploy` (helm upgrade --install), `up` (= cluster build load deploy), `test`, `lint`, `e2e`, `bench`. Each target should be one or two lines. Pin the ingress-nginx manifest URL to a specific release tag verified at build time.
- [ ] **Step 4:** Write the smoke test `assert orchestrator.constants.NS_PREFIX == "demo-"` and run `uv run pytest tests/unit -q` → PASS. Run `make lint` → PASS.
- [ ] **Step 5:** `make cluster`, then verify with `kubectl get nodes` (Ready) and `curl -s -o /dev/null -w '%{http_code}' http://localtest.me` → `404` (ingress-nginx default backend responding).
- [ ] **Step 6:** Commit `chore: scaffold project, toolchain, kind cluster`.

### Task 2: Durations and personas

**Files:** Create `src/orchestrator/core/durations.py`, `src/orchestrator/core/personas.py`, the three `personas/*/persona.yaml` (content per A5), `tests/unit/test_durations.py`, `tests/unit/test_personas.py`.

**Interfaces — Produces:**
```python
class InvalidDurationError(ValueError): ...
def parse_duration(text: str) -> timedelta          # raises InvalidDurationError
def format_duration(td: timedelta) -> str            # 5400s -> "1h30m"; 90s -> "1m30s"
class Brand(BaseModel): company_name: str; primary_color: str  # "#RRGGBB"
class Resources(BaseModel): cpu: str; memory: str; pods: int
class Fixtures(BaseModel): seed: int; locations: int; employees: int; roles: list[str]; certifications: list[str]; shift_pattern: Literal["12h","8h","split"]
class Persona(BaseModel): name: str; display_name: str; brand: Brand; default_ttl: timedelta; max_ttl: timedelta; resources: Resources; fixtures: Fixtures
    # validators: ttl strings parsed via parse_duration; default_ttl <= max_ttl <= 8h; name == folder name
class UnknownPersonaError(KeyError): ...
def load_personas(directory: Path) -> dict[str, Persona]
def get_persona(personas: dict[str, Persona], name: str) -> Persona  # UnknownPersonaError lists valid names
```

- [ ] **Step 1: Write failing tests**
```python
import pytest
from datetime import timedelta
from orchestrator.core.durations import parse_duration, format_duration, InvalidDurationError

@pytest.mark.parametrize("text,expected", [
    ("2h", timedelta(hours=2)), ("30m", timedelta(minutes=30)), ("90s", timedelta(seconds=90)),
    ("1h30m", timedelta(minutes=90)), ("8h", timedelta(hours=8)),
])
def test_parse_valid(text, expected):
    assert parse_duration(text) == expected

@pytest.mark.parametrize("text", ["", "0m", "0h0m0s", "-1h", "2H", "1d", "90", " 2h ", "2h 30m", "8h1s", "999h", "h", "m30"])
def test_parse_invalid_has_helpful_message(text):
    with pytest.raises(InvalidDurationError) as e:
        parse_duration(text)
    assert "e.g. 30m, 2h, 1h30m" in str(e.value)

@pytest.mark.parametrize("td,text", [(timedelta(minutes=90), "1h30m"), (timedelta(seconds=90), "1m30s"), (timedelta(hours=2), "2h")])
def test_format_roundtrip(td, text):
    assert format_duration(td) == text and parse_duration(text) == td
```
```python
from pathlib import Path
import pytest
from orchestrator.core.personas import load_personas, get_persona, UnknownPersonaError

REPO_PERSONAS = Path(__file__).parents[2] / "personas"

def test_ships_three_valid_personas():
    assert set(load_personas(REPO_PERSONAS)) == {"healthcare", "manufacturing", "restaurant"}

def test_unknown_persona_lists_valid_names():
    with pytest.raises(UnknownPersonaError, match="healthcare, manufacturing, restaurant"):
        get_persona(load_personas(REPO_PERSONAS), "retail")

def test_default_ttl_above_max_rejected(tmp_path):
    d = tmp_path / "bad"; d.mkdir()
    src = (REPO_PERSONAS / "healthcare" / "persona.yaml").read_text()
    (d / "persona.yaml").write_text(src.replace("name: healthcare", "name: bad").replace("default_ttl: 2h", "default_ttl: 9h"))
    with pytest.raises(ValueError):
        load_personas(tmp_path)
```
- [ ] **Step 2:** `uv run pytest tests/unit/test_durations.py tests/unit/test_personas.py -v` → FAIL (import errors).
- [ ] **Step 3:** Implement. `parse_duration` uses `re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)`, rejects empty or zero totals and totals > `HARD_MAX_TTL_SECONDS`, and its error message is `f"invalid duration {text!r}: use h/m/s units, e.g. 30m, 2h, 1h30m (max 8h)"`. Write the persona YAML files.
- [ ] **Step 4:** Tests → PASS. Lint and mypy → PASS.
- [ ] **Step 5:** Commit `feat(core): duration parsing and persona loading`.

### Task 3: Naming, expiry maths and the deletion guard

**Files:** Create `src/orchestrator/core/naming.py`, `core/expiry.py`, `core/guard.py`, `tests/unit/test_naming.py`, `test_expiry.py`, `test_guard.py`.

**Interfaces — Produces:**
```python
# naming.py
def generate_env_name(persona: str, rng: random.Random | None = None) -> str   # "healthcare-7f3k": persona + "-" + 4 chars [a-z0-9]
def namespace_for(env_name: str) -> str                                         # "demo-" + env_name; ValueError if result > 63 chars or not DNS-1123
# expiry.py
def utcnow() -> datetime                                                         # tz-aware; the ONLY clock source (monkeypatch in tests)
def to_rfc3339(dt: datetime) -> str; def from_rfc3339(s: str) -> datetime        # ValueError on naive/garbage
def compute_expires_at(created_at: datetime, ttl: timedelta) -> datetime
def is_expired(expires_at: datetime, now: datetime) -> bool                      # now >= expires_at
class TTLExceedsMaxError(ValueError): ...
def validate_total_ttl(ttl: timedelta, persona_max: timedelta) -> None           # raises if ttl > min(persona_max, 8h)
# guard.py
def is_deletable_namespace(name: str, labels: Mapping[str, str] | None) -> bool
```

- [ ] **Step 1: Write failing tests**
```python
# test_guard.py
import pytest
from orchestrator.core.guard import is_deletable_namespace
from orchestrator.constants import LABEL_MANAGED_BY, MANAGED_BY_VALUE
OK = {LABEL_MANAGED_BY: MANAGED_BY_VALUE}

def test_managed_demo_namespace_is_deletable():
    assert is_deletable_namespace("demo-healthcare-7f3k", OK)

@pytest.mark.parametrize("name,labels", [
    ("demo-healthcare-7f3k", None),                                  # no labels
    ("demo-healthcare-7f3k", {LABEL_MANAGED_BY: "helm"}),            # wrong manager
    ("healthcare-7f3k", OK),                                         # missing prefix
    ("kube-system", OK),                                             # protected + spoofed label
    ("default", OK),
    ("demo-orchestrator", OK),                                       # has prefix but protected
])
def test_everything_else_is_not(name, labels):
    assert not is_deletable_namespace(name, labels)
```
```python
# test_expiry.py
from datetime import datetime, timedelta, timezone
import pytest
from orchestrator.core.expiry import compute_expires_at, is_expired, validate_total_ttl, TTLExceedsMaxError, from_rfc3339, to_rfc3339
T0 = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)

def test_expiry_boundary():
    exp = compute_expires_at(T0, timedelta(hours=2))
    assert not is_expired(exp, exp - timedelta(milliseconds=1))
    assert is_expired(exp, exp)

def test_total_ttl_capped_by_persona_and_hard_max():
    validate_total_ttl(timedelta(hours=4), timedelta(hours=4))
    with pytest.raises(TTLExceedsMaxError):
        validate_total_ttl(timedelta(hours=5), timedelta(hours=4))

def test_rfc3339_roundtrip_and_rejects_naive():
    assert to_rfc3339(T0) == "2026-09-24T15:00:00.000Z"
    assert from_rfc3339(to_rfc3339(T0)) == T0
    with pytest.raises(ValueError):
        from_rfc3339("2026-09-24T15:00:00")      # naive
    with pytest.raises(ValueError):
        from_rfc3339("tomorrow")
```
```python
# test_naming.py
import random, re
import pytest
from orchestrator.core.naming import generate_env_name, namespace_for

def test_generated_names_are_dns_safe_and_deterministic_with_rng():
    a = generate_env_name("healthcare", random.Random(1)); b = generate_env_name("healthcare", random.Random(1))
    assert a == b and re.fullmatch(r"healthcare-[a-z0-9]{4}", a)

def test_namespace_for_rejects_too_long():
    with pytest.raises(ValueError):
        namespace_for("x" * 60)
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit `feat(core): naming, expiry maths, deletion guard`.

### Task 4: Ledger

**Files:** Create `src/orchestrator/core/ledger.py`, `tests/unit/test_ledger.py`.

**Interfaces — Produces:**
```python
class EventType(StrEnum): REQUESTED="requested"; READY="ready"; EXTENDED="extended"; EXPIRED="expired"; DELETED="deleted"; DELETE_TIMEOUT="delete_timeout"; FAILED="failed"; SWEEPER_REAPED="sweeper_reaped"; SWEEPER_SKIPPED="sweeper_skipped"
class LedgerEvent(BaseModel): ts: datetime; event: EventType; env: str; namespace: str; persona: str | None = None; actor: Literal["operator","sweeper","cli"]; details: dict[str, Any] = {}
class Ledger:
    def __init__(self, path: Path) -> None          # creates parent dirs
    def append(self, event: LedgerEvent) -> None    # os.open(O_WRONLY|O_APPEND|O_CREAT) + fcntl.flock(LOCK_EX), single write() of line + "\n"
    def read(self) -> Iterator[LedgerEvent]         # skips malformed lines with logging.warning; never raises on bad data
```

- [ ] **Step 1: Write failing tests**
```python
import multiprocessing as mp
from datetime import datetime, timezone
from orchestrator.core.ledger import Ledger, LedgerEvent, EventType

def _ev(i: int, actor="operator") -> LedgerEvent:
    return LedgerEvent(ts=datetime(2026,9,24,tzinfo=timezone.utc), event=EventType.REQUESTED, env=f"e{i}", namespace=f"demo-e{i}", actor=actor, details={"pad": "x" * 2000})

def _writer(path, actor, n):
    led = Ledger(path)
    for i in range(n): led.append(_ev(i, actor))

def test_roundtrip(tmp_path):
    led = Ledger(tmp_path / "l.jsonl"); led.append(_ev(1))
    assert [e.env for e in led.read()] == ["e1"]

def test_concurrent_writers_never_interleave(tmp_path):
    p = tmp_path / "l.jsonl"
    procs = [mp.Process(target=_writer, args=(p, a, 200)) for a in ("operator", "sweeper")]
    [x.start() for x in procs]; [x.join() for x in procs]
    events = list(Ledger(p).read())
    assert len(events) == 400
    assert len(p.read_text().splitlines()) == 400

def test_malformed_line_skipped(tmp_path, caplog):
    p = tmp_path / "l.jsonl"; led = Ledger(p); led.append(_ev(1))
    with p.open("a") as f: f.write('{"ts": "2026-09-24T00:00:00Z", "event": "rea\n')   # truncated write
    led.append(_ev(2))
    assert [e.env for e in led.read()] == ["e1", "e2"]
    assert "malformed" in caplog.text
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (serialize timestamps via `to_rfc3339`). **Step 4:** Run → PASS. **Step 5:** Commit `feat(core): append-only JSONL ledger with locking`.

### Task 5: Crewline demo app and seeder

**Files:** Create `src/demoapp/{app.py,db.py,models.py,fixtures.py,seed.py}`, `src/demoapp/templates/{base,dashboard,people,locations,shifts,certifications,payroll}.html`, `src/demoapp/static/app.css`, `docker/crewline.Dockerfile`, `tests/unit/test_fixtures.py`, `tests/unit/test_demoapp.py`.

**Interfaces — Produces:**
- Env: `DATABASE_URL`, `PERSONA_FILE` (path to mounted persona.yaml).
- `fixtures.build(persona: Persona) -> Dataset`, where `Dataset` holds lists of `Location`, `Person`, `Shift`, `Certification` dataclasses. It is deterministic for a given `fixtures.seed`, and certifications include ~10% expiring within 30 days of a fixed reference date (the seed date, not `now`), so screenshots are stable.
- `python -m demoapp.seed --persona-file <path>`: creates tables, inserts the dataset in one transaction, then writes a `seed_marker(persona, seeded_at, row_counts)` row. It is idempotent: if the marker exists, it exits 0 without duplicating data.
- HTTP: `/healthz` → 200 always; `/readyz` → 200 only if the DB is reachable and the marker exists, otherwise 503. Pages use `brand.primary_color` and `brand.company_name` and show a slim top banner: `Demo environment · <persona display_name> · expires <relative time>`. Expiry is read from env `EXPIRES_AT`, which the operator injects.

- [ ] **Step 1: Write failing tests.** Test that `fixtures.build` is deterministic (two calls give equal output) and matches `employees`/`locations` counts and the persona's roles. Test that every person has ≥1 certification from the persona list. Using `fastapi.testclient` against SQLite (`DATABASE_URL=sqlite://`): `/readyz` is 503 before seeding and 200 after `seed.run(...)`; running `seed.run` twice keeps the row count the same; `/people` HTML contains the brand company name.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. Keep SQL portable (SQLAlchemy Core/ORM, no Postgres-only features) so unit tests can use SQLite. **Step 4:** Run → PASS.
- [ ] **Step 5:** Dockerfile: `python:3.12-slim`, uv install of the non-dev deps, non-root user, `CMD uvicorn demoapp.app:app --host 0.0.0.0 --port 8080`. Build with `docker build -f docker/crewline.Dockerfile -t crewline:dev .` → succeeds.
- [ ] **Step 6:** Manual check: `docker run` against a throwaway `postgres:16-alpine`, run the seed, open the pages, and apply the `frontend-design` skill's guidance for a clean, credible SaaS look (it's the thing recorded in the demo video). Commit `feat(demoapp): Crewline sample app with persona seeding`.

### Task 6: CRD and manifest rendering

**Files:** Create `deploy/crd/demoenvironments.yaml`, `src/orchestrator/k8s/manifests.py`, `src/orchestrator/k8s/templates/{namespace,quota,limitrange,networkpolicy,persona-configmap,postgres,crewline,ingress,seed-job}.yaml.j2`, `tests/unit/test_manifests.py`.

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class EnvContext: env_name: str; namespace: str; persona: Persona; expires_at: datetime; owner_uid: str; base_domain: str; crewline_image: str = "crewline:dev"
def render_namespace(ctx: EnvContext) -> dict                  # labels + expires-at annotation + ownerReferences(apiVersion, kind, name, uid, controller=True, blockOwnerDeletion=True)
def render_workloads(ctx: EnvContext) -> list[dict]            # everything except namespace and seed job, in apply order
def render_seed_job(ctx: EnvContext) -> dict                   # backoffLimit: 2, activeDeadlineSeconds: 240, ttlSecondsAfterFinished unset (namespace deletion cleans up)
def url_for(ctx: EnvContext) -> str                            # f"http://{env_name}.{base_domain}"
```

- [ ] **Step 1: Write failing tests.** Namespace has all three labels plus the annotation equal to `to_rfc3339(expires_at)`. `ownerReferences[0].uid == owner_uid`. Every workload has `metadata.namespace == ctx.namespace`. The ResourceQuota matches the persona's resources. The NetworkPolicy has a default-deny policy. The Ingress host is `<env>.demo.localtest.me`. The crewline Deployment has readinessProbe `/readyz`, livenessProbe `/healthz` and env `EXPIRES_AT`. Every container sets resource requests and limits (otherwise the quota rejects pods; this is a real gotcha). Also assert that `deploy/crd/demoenvironments.yaml` loads and its `spec.ttl` pattern equals the Global Constraints regex.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement with Jinja2 `StrictUndefined` and `yaml.safe_load` of the rendered text. **Step 4:** Run → PASS.
- [ ] **Step 5:** `kubectl apply -f deploy/crd/demoenvironments.yaml`, then `kubectl apply` a sample CR. It should be accepted, and a CR with `ttl: 2H` should be rejected by the schema. Commit `feat(k8s): DemoEnvironment CRD and per-demo manifest rendering`.

### Task 7: Operator — provisioning

**Files:** Create `src/orchestrator/k8s/client.py`, `src/orchestrator/operator/{__init__.py,handlers.py,provision.py}`, `tests/unit/test_provision.py`, `tests/integration/conftest.py`, `tests/integration/test_provision_e2e.py`.

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:
```python
# provision.py (pure-ish; takes a K8s facade so it can be unit-tested with a fake)
class KubeFacade(Protocol):
    def apply(self, manifest: dict) -> None                           # server-side apply, fieldManager="demo-orchestrator"
    def deployment_available(self, ns: str, name: str) -> bool
    def job_status(self, ns: str, name: str) -> Literal["running","succeeded","failed"]
    def count_active_envs(self) -> int
def check_admission(spec: dict, personas: dict[str, Persona], active: int, max_envs: int) -> tuple[Persona, timedelta]  # raises AdmissionError(message)
# handlers.py
@kopf.on.create(GROUP, VERSION, PLURAL)   -> provisions; retries via kopf.TemporaryError(delay=3) while waiting; PermanentError on admission/timeout/seed failure
@kopf.on.startup()                         -> settings: persistent status storage on the CR, finalizer, posting level WARNING
```
- Behaviour: set `phase=Provisioning` and `createdAt=metadata.creationTimestamp`, and write the `requested` ledger event **only if the status doesn't already have `createdAt`**, so a retry after a restart doesn't duplicate it. Apply namespace → workloads, wait for the postgres and crewline Deployments, then set `phase=Seeding`, apply the seed Job and wait for it to succeed. Then wait for crewline `/readyz` (the Deployment's readiness gate covers this) and set `phase=Ready` with `readyAt`, `url`, `timings`, plus a `ready` ledger event. Timings come from `time.monotonic()` checkpoints stored in the status so they survive retries (store the start timestamps, not deltas). On failure: `phase=Failed`, message, `failed` ledger event, and `expiresAt = now + 10m` (namespace annotation updated too).
- [ ] **Step 1: Write failing unit tests** for `check_admission`: an unknown persona → message lists valid personas; a bad TTL → the duration message; TTL > persona max → rejected; `active >= max_envs` → `capacity: 5/5 environments in use`. Add a `FakeKube` test showing that a second `on_create` invocation with `createdAt` already present in the status writes no second `requested` event.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Unit tests → PASS.
- [ ] **Step 5: Integration test** (`@pytest.mark.integration`). The fixture applies the CRD and starts the operator as a subprocess (`uv run kopf run -m orchestrator.operator.handlers --all-namespaces`) with `LEDGER_PATH=tmp_path/...`. The test creates a CR `persona=healthcare, ttl=10m`, polls until `phase == Ready` (timeout 180 s), GETs `http://<name>.demo.localtest.me/people` → 200 containing "Riverbend Clinics", and checks the ledger has `requested` and `ready` events with `provisioning_seconds > 0`. Teardown deletes the CR. Run `make build load && uv run pytest -m integration tests/integration/test_provision_e2e.py -v` → PASS.
- [ ] **Step 6:** Commit `feat(operator): provision persona-seeded demo namespaces`.

### Task 8: Operator — expiry, extend and delete

**Files:** Modify `src/orchestrator/operator/handlers.py`. Create `tests/unit/test_expiry_handlers.py`, `tests/integration/test_expiry_e2e.py`.

**Interfaces — Produces:**
```python
@kopf.on.field(GROUP, VERSION, PLURAL, field="spec.ttl")  -> validate_total_ttl; recompute expiresAt = createdAt + ttl; patch status + namespace annotation; ledger "extended" {old_expires_at, new_expires_at}. If invalid: revert is NOT possible, so set status.message with the error and keep the old expiresAt.
@kopf.timer(GROUP, VERSION, PLURAL, interval=10, idle=0)    -> if is_expired(status.expiresAt, utcnow()): phase=Expiring, ledger "expired", delete the CR object
@kopf.on.delete(GROUP, VERSION, PLURAL)                    -> (finalizer) wait until namespace absent (TemporaryError delay=3), max 120 s → ledger "deleted" {lag_seconds: now - expiresAt, reason: "expired"|"manual"}; on timeout ledger "delete_timeout" and let the finalizer go (sweeper will retry)
```
- [ ] **Step 1: Write failing unit tests** with a frozen `utcnow`. The timer does nothing before `expiresAt` and deletes at/after it. An extend within the max updates both status and annotation and writes an `extended` event. An extend beyond the max leaves `expiresAt` unchanged and sets the message. A manual delete before expiry records `reason="manual"`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Unit tests → PASS.
- [ ] **Step 5: Integration test:** create a CR with `ttl=90s`, wait for Ready, then wait for the CR and namespace to be gone (timeout 90 + 120 s). The ledger shows `expired` then `deleted` with `lag_seconds <= 120`. Also: create with `ttl=5m`, patch `spec.ttl=6m`, and assert the namespace annotation moved forward by 60 s.
- [ ] **Step 6:** Commit `feat(operator): TTL expiry, extension, and finalizer-backed teardown`.

### Task 9: Helm chart and in-cluster deployment

**Files:** Create `charts/demo-orchestrator/{Chart.yaml,values.yaml,templates/*.yaml}`, `docker/orchestrator.Dockerfile`, and a `make chart-sync` rule copying `deploy/crd/*.yaml` and `personas/` into the chart (the chart's `personas` ConfigMap uses `.Files.Glob`).

- Operator Deployment: 1 replica, `strategy: Recreate` (avoids two operators during a rollout), env from values, ledger PVC at `/var/lib/orchestrator`, personas ConfigMap at `/etc/orchestrator/personas`, liveness via kopf's `--liveness=http://0.0.0.0:8080/healthz`.
- Sweeper CronJob: `schedule: "* * * * *"`, `concurrencyPolicy: Forbid`, `successfulJobsHistoryLimit: 1`, same PVC. Pod affinity to the operator pod so RWO works (document this).
- RBAC: operator ClusterRole = namespaces (all verbs), demoenvironments + `/status` (all), and in any namespace: deployments, services, configmaps, jobs, ingresses, networkpolicies, resourcequotas, limitranges (create/get/list/watch/patch/delete), plus events create. Sweeper ClusterRole per A8.
- Namespace for the chart: `demo-orchestrator` (in `PROTECTED_NAMESPACES`).
- [ ] **Step 1:** `helm lint charts/demo-orchestrator` → 0 failures. `helm template` output validates with `kubectl apply --dry-run=server`.
- [ ] **Step 2:** `make up` from a clean state (`make down` first), then `kubectl -n demo-orchestrator get pods` shows the operator Running, and `kubectl get cronjob -n demo-orchestrator` exists.
- [ ] **Step 3:** Make the integration fixture support `ORCH_MODE=incluster` (skip the subprocess and use the deployed operator; read the ledger via `kubectl cp`). Re-run Tasks 7–8's integration tests in that mode → PASS.
- [ ] **Step 4:** Commit `feat(deploy): Helm chart with least-privilege RBAC and shared ledger PVC`.

### Task 10: Sweeper (independent backstop)

**Files:** Create `src/orchestrator/core/sweep.py`, `src/orchestrator/sweeper/main.py`, `tests/unit/test_sweep.py`.

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class NsInfo: name: str; labels: Mapping[str, str]; annotations: Mapping[str, str]; created_at: datetime; phase: str   # phase: "Active"|"Terminating"
@dataclass(frozen=True)
class SweepAction: namespace: str; env: str | None; kind: Literal["reap_expired","reap_orphan","reap_unparseable","skip_terminating"]; reason: str
def plan_sweep(namespaces: Iterable[NsInfo], existing_envs: set[str], now: datetime, grace: timedelta) -> list[SweepAction]
```
Rules, in order, applied per namespace: not `is_deletable_namespace` → ignore silently (not even an action). `Terminating` → `skip_terminating`. Annotation parses and `now > expires_at + grace` → `reap_expired`. Env label not in `existing_envs` and age > 5 min → `reap_orphan`. Annotation missing or unparseable → `reap_unparseable` only if age > 8h + grace, otherwise ignore. `main.py` executes each action: delete the CR if it exists (lets the operator's finalizer log `deleted`; if the operator is down, patch out the finalizer after 30 s), then delete the namespace **re-checking the guard against a freshly fetched object** immediately before the call. Write `sweeper_reaped`/`sweeper_skipped` ledger events. Exit 0.

- [ ] **Step 1: Write failing tests**
```python
from datetime import datetime, timedelta, timezone
from orchestrator.core.sweep import plan_sweep, NsInfo
from orchestrator.constants import LABEL_MANAGED_BY, MANAGED_BY_VALUE, LABEL_ENV, ANNOTATION_EXPIRES_AT
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc); G = timedelta(seconds=120)

def ns(name, env, expires=None, age=timedelta(hours=1), labels=None, phase="Active", raw_ann=None):
    lab = labels if labels is not None else {LABEL_MANAGED_BY: MANAGED_BY_VALUE, LABEL_ENV: env}
    ann = {} if expires is None and raw_ann is None else {ANNOTATION_EXPIRES_AT: raw_ann or expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    return NsInfo(name, lab, ann, NOW - age, phase)

def kinds(actions): return {(a.namespace, a.kind) for a in actions}

def test_within_grace_is_left_for_operator():
    assert plan_sweep([ns("demo-a", "a", NOW - timedelta(seconds=60))], {"a"}, NOW, G) == []

def test_past_grace_is_reaped():
    assert kinds(plan_sweep([ns("demo-a", "a", NOW - timedelta(seconds=121))], {"a"}, NOW, G)) == {("demo-a", "reap_expired")}

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
    assert kinds(plan_sweep([fresh, ancient], {"e", "f"}, NOW, G)) == {("demo-f", "reap_unparseable")}

def test_terminating_is_skipped():
    assert kinds(plan_sweep([ns("demo-g", "g", NOW - timedelta(hours=1), phase="Terminating")], {"g"}, NOW, G)) == {("demo-g", "skip_terminating")}
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Manual: create an unmanaged namespace `demo-imposter` (no label) and a managed namespace with a past annotation and no CR. Run `uv run python -m orchestrator.sweeper.main` locally. Only the managed one is deleted, `demo-imposter` survives, and the ledger has one `sweeper_reaped` event. Clean up `demo-imposter`.
- [ ] **Step 6:** Commit `feat(sweeper): independent expiry backstop with guarded deletes`.

### Task 11: `democtl` CLI

**Files:** Create `src/orchestrator/cli/main.py`, `tests/unit/test_cli.py`.

**Interfaces:** Commands per A2 (except `metrics`/`bench`, which are Task 13). `create` generates a name unless `--name` is given, pre-validates the TTL and persona locally (same core functions, so errors match the operator's), creates the CR, and by default waits with a Rich spinner showing the live phase. `--no-wait` returns immediately. It exits 1 on `Failed` and prints `status.message`. `extend` reads the current `spec.ttl`, adds `--by`, and patches it. `list` shows expires-in as a relative time (`in 1h12m`, `expired`). All commands accept `--context` (kubeconfig context). Errors are one line and red, never a traceback, unless `DEMOCTL_DEBUG=1`.

- [ ] **Step 1: Write failing tests** using `typer.testing.CliRunner` with the K8s client monkeypatched to a fake: `create --ttl 2H` → exit 2 with the duration message and no API call; `create --persona retail` → lists valid personas; `extend` sends a patch with `spec.ttl == "2h30m"` from `2h` + `30m`; `extend` beyond the max → a clear error and no patch; `list` renders a row per CR with the correct expires-in for a frozen clock.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Manual against the cluster: create, list, extend, delete one healthcare env. Commit `feat(cli): democtl create/list/get/extend/delete/personas`.

### Task 12: Chaos and restart resilience e2e

**Files:** Create `tests/integration/test_chaos_e2e.py`. Adjust code only if these tests expose bugs.

- [ ] **Step 1: Operator down → sweeper reaps.** In-cluster mode: create an env with `ttl=90s` and wait for Ready. `kubectl scale deploy/demo-orchestrator-operator --replicas=0`. Wait ≤ 90 + 120 + 90 s. Assert the namespace is gone and the ledger contains `sweeper_reaped` for that env. Scale back to 1 and assert the operator starts cleanly: no crash loop, and the orphaned CR (if any) is finalized.
- [ ] **Step 2: Operator restart mid-provision.** Create an env, wait for `phase == Seeding`, then delete the operator pod. Assert the env still reaches Ready within 240 s, and the ledger has exactly one `requested` event and one `ready` event for it.
- [ ] **Step 3: Capacity.** With `MAX_CONCURRENT_ENVS=2` (helm `--set`), create three envs. The third goes to `Failed` with a `capacity:` message and is cleaned up ~10 min later (assert its `expiresAt` ≈ now + 10m rather than waiting).
- [ ] **Step 4:** Run `uv run pytest -m integration tests/integration/test_chaos_e2e.py -v` → PASS. Commit `test(e2e): operator outage, restart, and capacity resilience`.

### Task 13: Metrics, cost model and bench

**Files:** Create `src/orchestrator/core/metrics.py`, `src/orchestrator/core/pricing.py`, `pricing.yaml`, `src/orchestrator/cli/bench.py`. Modify `cli/main.py`. Create `tests/unit/test_metrics.py`.

**Interfaces — Produces:**
```python
@dataclass
class ProvisioningStats: n: int; p50: float; p95: float; max: float; mean_timings: dict[str, float]
@dataclass
class CleanupStats: expired: int; on_time: int; reliability: float; lag_p50: float; lag_p95: float; reaped_by_sweeper: int; delete_timeouts: int
@dataclass
class CostStats: on_demand_usd: float; baseline_usd: float; savings_pct: float; window_hours: float; pricing_source: str
@dataclass
class MetricsReport: provisioning: ProvisioningStats; cleanup: CleanupStats; cost: CostStats | None
def compute_metrics(events: Iterable[LedgerEvent], grace: timedelta = timedelta(seconds=120), since: datetime | None = None, pricing: Pricing | None = None, personas: dict[str, Persona] | None = None) -> MetricsReport
class Pricing(BaseModel): vcpu_hour_usd: float; gib_hour_usd: float; source_url: str; retrieved: date
def load_pricing(path: Path) -> Pricing
```
Percentiles use the nearest-rank method (document it). Envs with `requested` but no terminal event are excluded from cleanup stats and counted as `in_flight`. Envs that were `failed` are excluded from provisioning stats and counted separately.

- [ ] **Step 1: Write failing tests** on a hand-built event list: 4 envs with provisioning of 30/40/50/100 s → p50=40, p95=100, max=100. Cleanup: 3 expired, deleted with lags 5 s, 60 s and 300 s → reliability 2/3, one reaped by the sweeper. Cost (test personas each 1 vCPU / 1 GiB): 2 envs × 2 h at $0.05/vCPU-h and $0.005/GiB-h → on-demand $0.22. The baseline for 3 personas over a 24 h window → $3.96, and savings_pct matches. Empty ledger → n=0 with no ZeroDivisionError. `since` filters correctly.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. Fill `pricing.yaml` with real GKE Autopilot general-purpose list prices for one region, looked up now, with `source_url` and `retrieved` set. **Step 4:** Run → PASS.
- [ ] **Step 5:** `democtl metrics` renders a Rich table, or JSON with `--json`. `democtl bench --persona X --n N --ttl 2m --parallel 1` creates envs one at a time (respecting capacity), waits for Ready, and lets them expire naturally. At the end it prints the metrics filtered to the bench run (tag envs with `requestedBy=bench-<timestamp>` and filter on it).
- [ ] **Step 6:** Run `democtl bench --persona healthcare --n 10 --ttl 2m` and paste the output into `docs/results.md` with the date and machine specs. Commit `feat(metrics): ledger-derived provisioning, cleanup, and cost metrics + bench`.

### Task 14: CI, docs and packaging

**Files:** Create `.github/workflows/ci.yml`, `README.md`, `docs/architecture.md`, `docs/decisions.md`, `docs/demo-script.md`, `docs/postmortem.md` (skeleton only).

- [ ] **Step 1: CI.** Job `lint-test` (uv sync, ruff, mypy, pytest unit with coverage ≥ 85% on `orchestrator.core`). Job `e2e` (needs lint-test): kind via `helm/kind-action` using `deploy/kind/cluster.yaml`, build + load images, `make deploy`, then `pytest -m integration` excluding the slow chaos test, which runs on `workflow_dispatch` only. Push a branch and confirm both jobs are green.
- [ ] **Step 2: README.** One-paragraph pitch → Mermaid architecture diagram → a screenshot of Crewline in two personas side by side → quickstart (prereqs: Docker, kind, kubectl, helm, uv) → the CLI tour → the metrics table copied from `docs/results.md` (real numbers only; if there are none yet, say "run `democtl bench`") → a "Design decisions" link → a "What I'd do next" list (warm pool, GKE, Prometheus).
- [ ] **Step 3: `docs/decisions.md`.** Five short ADRs (context / decision / consequences): CRD+operator vs imperative script; namespace-per-demo vs shared pool; TTL from creation; two-layer expiry; JSONL ledger on an RWO PVC (and its multi-node limit).
- [ ] **Step 4: `docs/demo-script.md`.** A 2-minute shot list: `democtl personas` → create healthcare and manufacturing in parallel → show both branded apps → `kubectl get de` → `extend` → scale the operator to 0 and show the sweeper reaping → `democtl metrics`.
- [ ] **Step 5:** A final pass from a clean clone: `make down && make up && democtl create --persona restaurant --ttl 10m` works by following the README alone. Commit `docs: README, ADRs, demo script, CI`.

---

## Stretch goals (only after Task 14 is green; each gets its own plan)

- **S1 — Warm pool.** A `DemoPool` CR (`persona`, `size`) keeps N pre-provisioned, seeded, unclaimed namespaces. `create` claims one by relabelling, setting the annotation, and restarting crewline with the new `EXPIRES_AT`. The operator then replenishes the pool. Ledger `ready` details gain `mode: warm|cold`, and metrics split on it. **This is the measured version of the brief's "shared pool vs isolation" trade-off**, the strongest postmortem material.
- **S2 — Request portal.** A small FastAPI page in the operator chart: pick a persona and TTL → create → live phase → link. It's a nicer demo video than a terminal.
- **S3 — GKE.** Terraform or a `gcloud` script for an Autopilot cluster, Artifact Registry images, ledger moved to a GCS object-per-event, and Ingress via GKE Gateway. Include a teardown script and a budget alert. Record real cost against the model in `pricing.yaml`.
- **S4 — Observability.** Prometheus metrics from the operator (`demo_provision_seconds` histogram, `demo_active_envs` gauge, `demo_cleanup_lag_seconds`) and a Grafana dashboard JSON.

## Self-review record

- **Spec coverage:** A2 CLI → T11/T13; A3 CRD/status/TTL → T6–T8; A4 components → T4–T11; A5 personas → T2/T5; A6 ledger → T4; A7 metrics → T13; A8 guardrails → T3, T7 (capacity/timeout), T9 (RBAC), T10, T12; A10 deliverables → T14.
- **Review Focus → tests:** 1 → T3, T10 · 2 → T12 · 3 → T2 · 4 → T10 · 5 → T4.
- **Deliberately not code-complete:** infra tasks (T1, T9, T14) specify exact commands and acceptance checks rather than full YAML. The agent writes these against current tool versions, which shouldn't be recalled from memory.
