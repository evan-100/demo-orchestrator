# AGENTS.md

Demo Environment Orchestrator: a Python (kopf) Kubernetes operator that provisions persona-seeded,
TTL-bound demo namespaces and reliably tears them down.

**Source of truth:** `PROJECT.md`. Part A is the spec, Part B is the task-by-task plan. Work tasks in
order and tick the checkboxes as you go. If the spec and the plan conflict, the spec wins. Stop and ask.

## Rules
- TDD: write the failing test, watch it fail, implement, watch it pass, then commit. One task = at least one commit.
- Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest tests/unit -q`
- Integration tests: `uv run pytest -m integration` (needs `make up`). Never mark a task done while its integration test is red.
- Every namespace delete goes through `orchestrator.core.guard.is_deletable_namespace`. No exceptions.
- All times are tz-aware UTC. Get "now" only from `orchestrator.core.expiry.utcnow()`.
- Label/annotation/group strings come from `orchestrator/constants.py`. Never retype them.
- Pin dependency, image and manifest versions that are current *at build time*. Verify them, don't recall them.
- Never invent numbers (benchmarks, prices). Metrics come from `democtl bench`, and prices from a cited source.
- Keep the repo free of personal notes and private working files.
  Don't quote it anywhere.
- Conventional Commits (`feat(operator): ...`, `test(e2e): ...`).

## Commands
`make up` (kind + ingress + build + load + helm) · `make down` · `make test` · `make e2e` · `make bench`
