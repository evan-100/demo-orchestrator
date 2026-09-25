"""`democtl`: the CLI for creating, inspecting and tearing down demo environments.

Talks to the cluster through the `CliKube` seam (implemented by `KubeClient`,
faked in tests), and through the user's kubeconfig (`--context` selects the
context). Validation of the persona and TTL happens locally first, using the
same `orchestrator.core` functions the operator uses, so a rejected `create`
or `extend` fails with exactly the message the operator would have given —
without a round trip to the cluster.

Errors are a single red line on stderr and a process exit code: 2 for a usage
error (bad TTL, unknown persona, TTL over the persona max), 1 for a runtime
failure (the cluster couldn't be reached, the environment doesn't exist, or it
reached `Failed`). Set `DEMOCTL_DEBUG=1` to see the underlying exception and
its traceback instead of the one-line message.
"""

from __future__ import annotations

import getpass
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, NoReturn, Protocol

import typer
from rich.console import Console
from rich.table import Table

from orchestrator.config import get_settings
from orchestrator.core.durations import (
    InvalidDurationError,
    format_duration,
    parse_duration,
)
from orchestrator.core.expiry import TTLExceedsMaxError, from_rfc3339, utcnow, validate_total_ttl
from orchestrator.core.naming import generate_env_name, namespace_for
from orchestrator.core.personas import UnknownPersonaError, get_persona, load_personas
from orchestrator.k8s.client import KubeClient, NamespaceInfo, load_kube_config

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage demo environments.")


def _make_console(*, stderr: bool = False) -> Console:
    """A real terminal keeps its detected width; anything else (pipes, tests)
    gets a wide fixed width so Rich doesn't fall back to 80 columns and
    truncate columns like URL."""
    out = Console(stderr=stderr)
    if not out.is_terminal:
        out.width = 140
    return out


console = _make_console()
err_console = _make_console(stderr=True)

POLL_INTERVAL_SECONDS = 2.0
# Just above the operator's own PROVISION_TIMEOUT (300s default), so a create
# that's going to fail has usually already gone to Failed before we'd time out.
CREATE_TIMEOUT_SECONDS = 360
EXTEND_WAIT_SECONDS = 10
DELETE_WAIT_SECONDS = 120


class CliKube(Protocol):
    """The slice of the Kubernetes API `democtl` needs, faked in tests."""

    def create_env(self, name: str, spec: dict[str, Any]) -> None: ...

    def get_env(self, name: str) -> dict[str, Any] | None: ...

    def list_envs(self) -> list[dict[str, Any]]: ...

    def patch_env_spec(self, name: str, spec: dict[str, Any]) -> None: ...

    def delete_env(self, name: str) -> None: ...

    def get_namespace(self, name: str) -> NamespaceInfo | None: ...


def _default_get_kube(context: str | None) -> CliKube:
    load_kube_config(context)
    return KubeClient()


# Tests monkeypatch this name to inject a fake `CliKube`.
get_kube: Callable[[str | None], CliKube] = _default_get_kube


class UsageError(RuntimeError):
    """A validation failure (exit code 2), raised instead of exiting when debugging."""


class RuntimeFailure(RuntimeError):
    """A runtime failure (exit code 1), raised instead of exiting when debugging."""


def _debug_enabled() -> bool:
    return os.environ.get("DEMOCTL_DEBUG") == "1"


def _usage_error(message: str) -> NoReturn:
    if _debug_enabled():
        raise UsageError(message)
    err_console.print(message, style="bold red")
    raise typer.Exit(code=2)


def _runtime_error(message: str) -> NoReturn:
    if _debug_enabled():
        raise RuntimeFailure(message)
    err_console.print(message, style="bold red")
    raise typer.Exit(code=1)


def relative_expiry(expires_at: datetime, now: datetime) -> str:
    """Format `expires_at` relative to `now`: "in 1h12m", "in 42s", or "expired"."""
    if expires_at <= now:
        return "expired"
    total = int((expires_at - now).total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"in {hours}h{minutes}m"
    if minutes:
        return f"in {minutes}m"
    return f"in {seconds}s"


def format_age(created_at: datetime, now: datetime) -> str:
    """Format the elapsed time since `created_at` as compact h/m/s, e.g. "1h12m"."""
    total = max(int((now - created_at).total_seconds()), 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


@app.command()
def personas(
    context: str | None = typer.Option(None, "--context", help="kubeconfig context (unused)"),
) -> None:
    """List the available personas."""
    del context  # personas are local data; kept for a consistent CLI surface
    settings = get_settings()
    try:
        loaded = load_personas(settings.personas_dir)
    except Exception as exc:
        _runtime_error(f"failed to load personas from {settings.personas_dir}: {exc}")
        return
    if not loaded:
        console.print("No personas configured.")
        return
    table = Table()
    table.add_column("NAME")
    table.add_column("DISPLAY NAME")
    table.add_column("DEFAULT TTL")
    table.add_column("MAX TTL")
    for name in sorted(loaded):
        persona = loaded[name]
        table.add_row(
            name,
            persona.display_name,
            format_duration(persona.default_ttl),
            format_duration(persona.max_ttl),
        )
    console.print(table)


def _print_ready(status: dict[str, Any], name: str) -> None:
    url = status.get("url", "")
    expires_at_s = status.get("expiresAt")
    expiry_display = "unknown"
    if expires_at_s:
        expiry_display = f"{expires_at_s} ({relative_expiry(from_rfc3339(expires_at_s), utcnow())})"
    console.print(f"[bold green]Ready[/bold green]  {name}")
    console.print(f"  url:     {url}")
    console.print(f"  expires: {expiry_display}")


def _wait_for_ready(kube: CliKube, name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    final_status: dict[str, Any] = {"phase": "Timeout"}
    with console.status(f"waiting for {name}...", spinner="dots") as spinner:
        while True:
            try:
                env = kube.get_env(name)
            except Exception as exc:
                _runtime_error(f"failed to get {name}: {exc}")
                return
            if env is None:
                # R16: a CR that vanishes mid-wait (deleted, or expired before
                # ever reaching Ready) must not be polled as "Pending" until
                # the timeout — that would misreport a disappearance as a
                # provisioning delay.
                _runtime_error(f"{name} disappeared while waiting (deleted or expired)")
                return
            status = env.get("status") or {}
            phase = status.get("phase", "Pending")
            spinner.update(f"[cyan]{phase}[/cyan] — {name}")
            if phase in ("Ready", "Failed"):
                final_status = status
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_INTERVAL_SECONDS)
    phase = final_status.get("phase")
    if phase == "Ready":
        _print_ready(final_status, name)
    elif phase == "Failed":
        _runtime_error(final_status.get("message") or f"{name} failed")
    else:
        _runtime_error(f"timed out waiting for {name} to become Ready after {timeout}s")


@app.command()
def create(
    persona: str = typer.Option(..., "--persona", help="Persona name"),
    ttl: str | None = typer.Option(None, "--ttl", help="Time-to-live, e.g. 30m, 2h, 1h30m"),
    name: str | None = typer.Option(None, "--name", help="Environment name (generated if omitted)"),
    requested_by: str | None = typer.Option(
        None, "--requested-by", help="Requester identity (defaults to the OS user)"
    ),
    context: str | None = typer.Option(None, "--context", help="kubeconfig context"),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Wait for the environment to be Ready"
    ),
    timeout: int = typer.Option(
        CREATE_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for Ready"
    ),
) -> None:
    """Create a demo environment."""
    settings = get_settings()
    try:
        loaded_personas = load_personas(settings.personas_dir)
    except Exception as exc:
        _runtime_error(f"failed to load personas from {settings.personas_dir}: {exc}")
        return

    try:
        chosen = get_persona(loaded_personas, persona)
    except UnknownPersonaError as exc:
        _usage_error(str(exc.args[0]))
        return

    ttl_text = ttl if ttl is not None else format_duration(chosen.default_ttl)
    try:
        ttl_delta = parse_duration(ttl_text)
        validate_total_ttl(ttl_delta, chosen.max_ttl)
    except (InvalidDurationError, TTLExceedsMaxError) as exc:
        _usage_error(str(exc))
        return

    env_name = name if name is not None else generate_env_name(chosen.name)
    requester = requested_by if requested_by is not None else getpass.getuser()
    spec = {"persona": chosen.name, "ttl": ttl_text, "requestedBy": requester}

    kube = get_kube(context)
    try:
        kube.create_env(env_name, spec)
    except Exception as exc:
        _runtime_error(f"failed to create {env_name}: {exc}")
        return

    console.print(f"[green]Created[/green] {env_name} (persona={chosen.name}, ttl={ttl_text})")

    if not wait:
        return
    _wait_for_ready(kube, env_name, timeout)


@app.command(name="list")
def list_envs(
    context: str | None = typer.Option(None, "--context", help="kubeconfig context"),
) -> None:
    """List demo environments."""
    kube = get_kube(context)
    try:
        envs = kube.list_envs()
    except Exception as exc:
        _runtime_error(f"failed to list demo environments: {exc}")
        return

    if not envs:
        console.print("No demo environments.")
        return

    now = utcnow()
    table = Table()
    table.add_column("NAME")
    table.add_column("PERSONA")
    table.add_column("PHASE")
    table.add_column("AGE")
    table.add_column("EXPIRES-IN")
    table.add_column("URL")
    for env in envs:
        meta = env.get("metadata") or {}
        spec = env.get("spec") or {}
        status = env.get("status") or {}
        created_s = meta.get("creationTimestamp")
        age = format_age(from_rfc3339(created_s), now) if created_s else "-"
        expires_s = status.get("expiresAt")
        expires_in = relative_expiry(from_rfc3339(expires_s), now) if expires_s else "-"
        table.add_row(
            meta.get("name", ""),
            spec.get("persona", ""),
            status.get("phase", "Pending"),
            age,
            expires_in,
            status.get("url", ""),
        )
    console.print(table)


@app.command()
def get(
    name: str = typer.Argument(..., help="Environment name"),
    context: str | None = typer.Option(None, "--context", help="kubeconfig context"),
) -> None:
    """Show the full status of one demo environment."""
    kube = get_kube(context)
    try:
        env = kube.get_env(name)
    except Exception as exc:
        _runtime_error(f"failed to get {name}: {exc}")
        return
    if env is None:
        _runtime_error(f"no such demo environment: {name}")
        return

    spec = env.get("spec") or {}
    status = env.get("status") or {}
    console.print(f"[bold]{name}[/bold]")
    console.print(f"  persona:      {spec.get('persona', '')}")
    console.print(f"  ttl:          {spec.get('ttl', '')}")
    console.print(f"  requestedBy:  {spec.get('requestedBy', '')}")
    console.print(f"  phase:        {status.get('phase', '')}")
    console.print(f"  namespace:    {status.get('namespace', '')}")
    console.print(f"  url:          {status.get('url', '')}")
    console.print(f"  message:      {status.get('message', '')}")
    console.print(f"  createdAt:    {status.get('createdAt', '')}")
    console.print(f"  readyAt:      {status.get('readyAt', '')}")
    console.print(f"  expiresAt:    {status.get('expiresAt', '')}")
    timings = status.get("timings") or {}
    if timings:
        console.print("  timings:")
        for key in ("namespaceSeconds", "appReadySeconds", "seedSeconds", "totalSeconds"):
            if key in timings:
                console.print(f"    {key}: {timings[key]}")


def _wait_for_new_expiry(
    kube: CliKube, name: str, old_expires_at: str | None, timeout: int
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            env = kube.get_env(name)
        except Exception as exc:
            _runtime_error(f"failed to get {name}: {exc}")
            return
        status = (env or {}).get("status") or {}
        expires_at_s = status.get("expiresAt")
        if expires_at_s and expires_at_s != old_expires_at:
            relative = relative_expiry(from_rfc3339(expires_at_s), utcnow())
            console.print(f"  new expiry: {expires_at_s} ({relative})")
            return
        time.sleep(POLL_INTERVAL_SECONDS)


@app.command()
def extend(
    name: str = typer.Argument(..., help="Environment name"),
    by: str = typer.Option(..., "--by", help="Amount to add to the TTL, e.g. 30m"),
    context: str | None = typer.Option(None, "--context", help="kubeconfig context"),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Wait briefly for the new expiry to appear"
    ),
    timeout: int = typer.Option(
        EXTEND_WAIT_SECONDS, "--timeout", help="Seconds to wait for the new expiry"
    ),
) -> None:
    """Extend a demo environment's TTL."""
    settings = get_settings()
    kube = get_kube(context)
    try:
        env = kube.get_env(name)
    except Exception as exc:
        _runtime_error(f"failed to get {name}: {exc}")
        return
    if env is None:
        _runtime_error(f"no such demo environment: {name}")
        return

    spec = env.get("spec") or {}
    status = env.get("status") or {}

    try:
        loaded_personas = load_personas(settings.personas_dir)
        persona = get_persona(loaded_personas, str(spec.get("persona", "")))
    except UnknownPersonaError as exc:
        _runtime_error(str(exc.args[0]))
        return
    except Exception as exc:
        _runtime_error(f"failed to load personas from {settings.personas_dir}: {exc}")
        return

    try:
        current_ttl = parse_duration(str(spec.get("ttl", "")))
        by_delta = parse_duration(by)
    except InvalidDurationError as exc:
        _usage_error(str(exc))
        return

    new_ttl = current_ttl + by_delta
    try:
        validate_total_ttl(new_ttl, persona.max_ttl)
    except TTLExceedsMaxError as exc:
        _usage_error(str(exc))
        return

    new_ttl_text = format_duration(new_ttl)
    old_expires_at = status.get("expiresAt")
    try:
        kube.patch_env_spec(name, {"ttl": new_ttl_text})
    except Exception as exc:
        _runtime_error(f"failed to extend {name}: {exc}")
        return

    console.print(f"[green]Extended[/green] {name}: requested ttl={new_ttl_text}")
    if wait:
        _wait_for_new_expiry(kube, name, old_expires_at, timeout)


def _wait_for_namespace_gone(kube: CliKube, namespace: str, timeout: int) -> bool:
    """Poll until `namespace` is gone. Returns False on a plain timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            ns = kube.get_namespace(namespace)
        except Exception as exc:
            _runtime_error(f"failed to check namespace {namespace}: {exc}")
        if ns is None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)


@app.command()
def delete(
    name: str = typer.Argument(..., help="Environment name"),
    context: str | None = typer.Option(None, "--context", help="kubeconfig context"),
    wait: bool = typer.Option(False, "--wait", help="Wait for the namespace to disappear"),
    timeout: int = typer.Option(
        DELETE_WAIT_SECONDS, "--timeout", help="Seconds to wait for teardown"
    ),
) -> None:
    """Delete a demo environment (the operator's finalizer tears it down)."""
    kube = get_kube(context)
    try:
        env = kube.get_env(name)
    except Exception as exc:
        _runtime_error(f"failed to get {name}: {exc}")
        return

    namespace = (env.get("status") or {}).get("namespace") if env else None
    if namespace is None:
        try:
            namespace = namespace_for(name)
        except ValueError:
            namespace = None

    try:
        kube.delete_env(name)
    except Exception as exc:
        _runtime_error(f"failed to delete {name}: {exc}")
        return

    console.print(f"[green]Deleted[/green] {name}")

    if not wait or namespace is None:
        return

    with console.status(f"waiting for namespace {namespace} to disappear...", spinner="dots"):
        gone = _wait_for_namespace_gone(kube, namespace, timeout)

    if not gone:
        _runtime_error(f"timed out waiting for namespace {namespace} to be deleted")
        return
    console.print(f"[green]Namespace {namespace} gone[/green]")


if __name__ == "__main__":
    app()
