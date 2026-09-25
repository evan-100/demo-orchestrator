"""Unit tests for `democtl`, driven through `typer.testing.CliRunner`.

The Kubernetes seam (`cli.main.get_kube`) is monkeypatched to an in-memory
`FakeKube`, and persona loading (`cli.main.get_settings`) points at the
repo's real `personas/` directory, so validation errors match the operator's
exactly. The clock (`cli.main.utcnow`) is frozen for the `list` tests.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestrator.cli import main as cli_main
from orchestrator.config import Settings
from orchestrator.core.expiry import to_rfc3339
from orchestrator.k8s.client import NamespaceInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = REPO_ROOT / "personas"

runner = CliRunner()


class FakeKube:
    """In-memory stand-in for `CliKube`."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.envs: dict[str, dict[str, Any]] = {}
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.namespaces: dict[str, NamespaceInfo] = {}
        # The status a freshly created env is given; tests override this to
        # simulate the operator having already moved it to Failed.
        self.create_status: dict[str, Any] = {"phase": "Pending"}

    def create_env(self, name: str, spec: dict[str, Any]) -> None:
        self.created.append((name, dict(spec)))
        self.envs[name] = {
            "metadata": {"name": name, "creationTimestamp": to_rfc3339(cli_main.utcnow())},
            "spec": dict(spec),
            "status": dict(self.create_status),
        }

    def get_env(self, name: str) -> dict[str, Any] | None:
        env = self.envs.get(name)
        return copy.deepcopy(env) if env is not None else None

    def list_envs(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(e) for e in self.envs.values()]

    def patch_env_spec(self, name: str, spec: dict[str, Any]) -> None:
        self.patched.append((name, dict(spec)))
        self.envs[name]["spec"].update(spec)

    def delete_env(self, name: str) -> None:
        self.deleted.append(name)
        self.envs.pop(name, None)

    def get_namespace(self, name: str) -> NamespaceInfo | None:
        return self.namespaces.get(name)


@pytest.fixture
def fake_kube(monkeypatch: pytest.MonkeyPatch) -> FakeKube:
    kube = FakeKube()
    monkeypatch.setattr(cli_main, "get_kube", lambda context=None: kube)
    return kube


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    settings = Settings(personas_dir=PERSONAS_DIR, ledger_path=tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wait loops poll `time.sleep`; nothing in these tests needs it to be real.
    monkeypatch.setattr(cli_main.time, "sleep", lambda _seconds: None)


# --- create ------------------------------------------------------------


def test_create_bad_ttl_exits_2_with_no_api_call(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["create", "--persona", "healthcare", "--ttl", "2H"])

    assert result.exit_code == 2
    assert "invalid duration" in result.output
    assert "e.g. 30m, 2h, 1h30m" in result.output
    assert fake_kube.created == []


def test_create_unknown_persona_lists_valid_names(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["create", "--persona", "retail", "--no-wait"])

    assert result.exit_code == 2
    assert "unknown persona 'retail'" in result.output
    for expected in ("healthcare", "manufacturing", "restaurant"):
        assert expected in result.output
    assert fake_kube.created == []


def test_create_ttl_over_persona_max_exits_2(
    fake_kube: FakeKube, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # All three shipped personas cap out at the 8h hard ceiling, so a TTL that
    # is a *valid duration* but still over a persona's own max needs a persona
    # with a lower max_ttl than 8h.
    persona_dir = tmp_path / "personas" / "lowmax"
    persona_dir.mkdir(parents=True)
    (persona_dir / "persona.yaml").write_text(
        "name: lowmax\n"
        "display_name: Low Max\n"
        "brand: { company_name: Low Max Co, primary_color: '#123456' }\n"
        "default_ttl: 15m\n"
        "max_ttl: 1h\n"
        "resources: { cpu: '1', memory: 1Gi, pods: 10 }\n"
        "fixtures:\n"
        "  seed: 1\n"
        "  locations: 1\n"
        "  employees: 1\n"
        "  roles: [Role]\n"
        "  certifications: [Cert]\n"
        "  shift_pattern: 8h\n"
    )
    monkeypatch.setattr(
        cli_main,
        "get_settings",
        lambda: Settings(personas_dir=tmp_path / "personas", ledger_path=tmp_path / "ledger.jsonl"),
    )

    result = runner.invoke(
        cli_main.app, ["create", "--persona", "lowmax", "--ttl", "2h", "--no-wait"]
    )

    assert result.exit_code == 2
    assert "exceeds the maximum" in result.output
    assert fake_kube.created == []


def test_create_no_wait_sends_expected_spec(
    fake_kube: FakeKube, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main.getpass, "getuser", lambda: "evan")

    result = runner.invoke(
        cli_main.app,
        ["create", "--persona", "healthcare", "--name", "healthcare-test", "--no-wait"],
    )

    assert result.exit_code == 0, result.output
    assert len(fake_kube.created) == 1
    name, spec = fake_kube.created[0]
    assert name == "healthcare-test"
    assert spec == {"persona": "healthcare", "ttl": "2h", "requestedBy": "evan"}


def test_create_no_wait_uses_requested_by_and_ttl_overrides(fake_kube: FakeKube) -> None:
    result = runner.invoke(
        cli_main.app,
        [
            "create",
            "--persona",
            "healthcare",
            "--name",
            "healthcare-x",
            "--ttl",
            "45m",
            "--requested-by",
            "someone",
            "--no-wait",
        ],
    )

    assert result.exit_code == 0, result.output
    _, spec = fake_kube.created[0]
    assert spec == {"persona": "healthcare", "ttl": "45m", "requestedBy": "someone"}


def test_create_waits_and_exits_1_on_failed(fake_kube: FakeKube) -> None:
    fake_kube.create_status = {
        "phase": "Failed",
        "message": "capacity: 5/5 environments in use",
    }

    result = runner.invoke(
        cli_main.app, ["create", "--persona", "healthcare", "--name", "healthcare-full"]
    )

    assert result.exit_code == 1
    assert "capacity: 5/5 environments in use" in result.output


def test_create_waits_and_prints_url_and_expiry_on_ready(fake_kube: FakeKube) -> None:
    fake_kube.create_status = {
        "phase": "Ready",
        "url": "http://healthcare-ready.demo.localtest.me",
        "expiresAt": to_rfc3339(cli_main.utcnow() + timedelta(hours=2)),
    }

    result = runner.invoke(
        cli_main.app, ["create", "--persona", "healthcare", "--name", "healthcare-ready"]
    )

    assert result.exit_code == 0, result.output
    assert "Ready" in result.output
    assert "http://healthcare-ready.demo.localtest.me" in result.output


def test_create_wait_get_env_raises_exits_1_with_no_traceback(fake_kube: FakeKube) -> None:
    """A poll that starts fine but breaks partway through must fail cleanly, not crash."""
    fake_kube.create_status = {"phase": "Pending"}
    calls = {"n": 0}
    real_get_env = fake_kube.get_env

    def flaky_get_env(name: str) -> dict[str, Any] | None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise TimeoutError("connection reset")
        return real_get_env(name)

    fake_kube.get_env = flaky_get_env  # type: ignore[method-assign]

    result = runner.invoke(
        cli_main.app, ["create", "--persona", "healthcare", "--name", "healthcare-flaky"]
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "connection reset" in result.output
    assert "Traceback" not in result.output


def test_create_wait_env_disappearing_exits_1(fake_kube: FakeKube) -> None:
    """R16: a CR gone mid-wait (deleted or expired) fails fast, not as 'Pending' to the timeout."""
    fake_kube.create_status = {"phase": "Pending"}
    calls = {"n": 0}
    real_get_env = fake_kube.get_env

    def disappearing_get_env(name: str) -> dict[str, Any] | None:
        calls["n"] += 1
        if calls["n"] >= 2:
            return None
        return real_get_env(name)

    fake_kube.get_env = disappearing_get_env  # type: ignore[method-assign]

    result = runner.invoke(
        cli_main.app, ["create", "--persona", "healthcare", "--name", "healthcare-vanish"]
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "healthcare-vanish disappeared while waiting (deleted or expired)" in result.output


# --- extend --------------------------------------------------------------


def _seed_env(
    fake_kube: FakeKube,
    name: str,
    *,
    persona: str = "healthcare",
    ttl: str = "2h",
    expires_at: str | None = None,
    namespace: str | None = None,
) -> None:
    fake_kube.envs[name] = {
        "metadata": {"name": name, "creationTimestamp": to_rfc3339(cli_main.utcnow())},
        "spec": {"persona": persona, "ttl": ttl},
        "status": {
            "phase": "Ready",
            "namespace": namespace or f"demo-{name}",
            "expiresAt": expires_at,
        },
    }


def test_extend_sends_patch_with_summed_ttl(fake_kube: FakeKube) -> None:
    _seed_env(fake_kube, "healthcare-ab12", ttl="2h")

    result = runner.invoke(cli_main.app, ["extend", "healthcare-ab12", "--by", "30m", "--no-wait"])

    assert result.exit_code == 0, result.output
    assert fake_kube.patched == [("healthcare-ab12", {"ttl": "2h30m"})]


def test_extend_wait_get_env_raises_exits_1_with_no_traceback(fake_kube: FakeKube) -> None:
    """The patch already succeeded; a broken poll afterwards must still fail cleanly."""
    _seed_env(
        fake_kube,
        "healthcare-ab12",
        ttl="2h",
        expires_at=to_rfc3339(cli_main.utcnow() + timedelta(hours=2)),
    )
    calls = {"n": 0}
    real_get_env = fake_kube.get_env

    def flaky_get_env(name: str) -> dict[str, Any] | None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise TimeoutError("api down")
        return real_get_env(name)

    fake_kube.get_env = flaky_get_env  # type: ignore[method-assign]

    result = runner.invoke(cli_main.app, ["extend", "healthcare-ab12", "--by", "30m"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "api down" in result.output
    assert "Traceback" not in result.output
    # The patch itself must still have gone through before the wait broke.
    assert fake_kube.patched == [("healthcare-ab12", {"ttl": "2h30m"})]


def test_extend_beyond_max_is_rejected_with_no_patch(fake_kube: FakeKube) -> None:
    _seed_env(fake_kube, "healthcare-ab12", ttl="7h50m")

    result = runner.invoke(cli_main.app, ["extend", "healthcare-ab12", "--by", "30m", "--no-wait"])

    assert result.exit_code == 2
    assert "exceeds the maximum" in result.output
    assert fake_kube.patched == []


def test_extend_missing_env_exits_1(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["extend", "nope", "--by", "30m", "--no-wait"])

    assert result.exit_code == 1
    assert "no such demo environment" in result.output
    assert fake_kube.patched == []


# --- list ------------------------------------------------------------------


NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)


def test_list_is_empty_by_default(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["list"])

    assert result.exit_code == 0
    assert "No demo environments." in result.output


def test_list_renders_row_with_expires_in_for_frozen_clock(
    fake_kube: FakeKube, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "utcnow", lambda: NOW)
    fake_kube.envs["healthcare-ab12"] = {
        "metadata": {
            "name": "healthcare-ab12",
            "creationTimestamp": to_rfc3339(NOW - timedelta(hours=1, minutes=12)),
        },
        "spec": {"persona": "healthcare", "ttl": "2h"},
        "status": {
            "phase": "Ready",
            "expiresAt": to_rfc3339(NOW + timedelta(hours=1, minutes=12)),
            "url": "http://healthcare-ab12.demo.localtest.me",
        },
    }
    fake_kube.envs["healthcare-expired"] = {
        "metadata": {
            "name": "healthcare-expired",
            "creationTimestamp": to_rfc3339(NOW - timedelta(hours=3)),
        },
        "spec": {"persona": "healthcare", "ttl": "2h"},
        "status": {"phase": "Expiring", "expiresAt": to_rfc3339(NOW - timedelta(minutes=5))},
    }

    result = runner.invoke(cli_main.app, ["list"])

    assert result.exit_code == 0, result.output
    assert "healthcare-ab12" in result.output
    assert "1h12m" in result.output  # age
    assert "in 1h12m" in result.output  # expires-in
    assert "healthcare-expired" in result.output
    assert "expired" in result.output


# --- get ---------------------------------------------------------------


def test_get_missing_env_exits_1_with_one_line_error(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["get", "nope"])

    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "no such demo environment: nope" in lines[0]


def test_get_shows_full_status(fake_kube: FakeKube) -> None:
    _seed_env(fake_kube, "healthcare-ab12", expires_at=to_rfc3339(cli_main.utcnow()))
    fake_kube.envs["healthcare-ab12"]["status"]["timings"] = {"totalSeconds": 41.7}

    result = runner.invoke(cli_main.app, ["get", "healthcare-ab12"])

    assert result.exit_code == 0, result.output
    assert "phase:" in result.output
    assert "totalSeconds: 41.7" in result.output


# --- personas ------------------------------------------------------------


def test_personas_lists_shipped_personas(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["personas"])

    assert result.exit_code == 0
    for expected in ("healthcare", "manufacturing", "restaurant"):
        assert expected in result.output


# --- delete --------------------------------------------------------------


def test_delete_deletes_the_cr(fake_kube: FakeKube) -> None:
    _seed_env(fake_kube, "healthcare-ab12", namespace="demo-healthcare-ab12")

    result = runner.invoke(cli_main.app, ["delete", "healthcare-ab12"])

    assert result.exit_code == 0, result.output
    assert fake_kube.deleted == ["healthcare-ab12"]
    assert "Deleted" in result.output


def test_delete_wait_polls_until_namespace_gone(fake_kube: FakeKube) -> None:
    _seed_env(fake_kube, "healthcare-ab12", namespace="demo-healthcare-ab12")
    calls = {"n": 0}
    real_get_namespace = fake_kube.get_namespace

    def flaky_get_namespace(name: str) -> NamespaceInfo | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return NamespaceInfo(name=name, uid="u", resource_version="1")
        return real_get_namespace(name)

    fake_kube.get_namespace = flaky_get_namespace  # type: ignore[method-assign]

    result = runner.invoke(cli_main.app, ["delete", "healthcare-ab12", "--wait"])

    assert result.exit_code == 0, result.output
    assert "gone" in result.output


def test_delete_wait_get_namespace_raises_exits_1_with_no_traceback(fake_kube: FakeKube) -> None:
    """The CR is already deleted; a broken poll for teardown must still fail cleanly."""
    _seed_env(fake_kube, "healthcare-ab12", namespace="demo-healthcare-ab12")
    calls = {"n": 0}

    def flaky_get_namespace(name: str) -> NamespaceInfo | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return NamespaceInfo(name=name, uid="u", resource_version="1")
        raise TimeoutError("etcd unavailable")

    fake_kube.get_namespace = flaky_get_namespace  # type: ignore[method-assign]

    result = runner.invoke(cli_main.app, ["delete", "healthcare-ab12", "--wait"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "etcd unavailable" in result.output
    assert "Traceback" not in result.output
    # The delete_env call itself must still have gone through.
    assert fake_kube.deleted == ["healthcare-ab12"]


# --- DEMOCTL_DEBUG ---------------------------------------------------------


def test_debug_env_shows_traceback_instead_of_one_liner(
    fake_kube: FakeKube, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEMOCTL_DEBUG", "1")

    result = runner.invoke(cli_main.app, ["create", "--persona", "healthcare", "--ttl", "2H"])

    assert result.exception is not None
    assert isinstance(result.exception, cli_main.UsageError)
    assert "invalid duration" in str(result.exception)
    # No formatted one-line error was printed; the exception propagated instead.
    assert "invalid duration" not in result.output


def test_without_debug_no_exception_propagates(fake_kube: FakeKube) -> None:
    result = runner.invoke(cli_main.app, ["create", "--persona", "healthcare", "--ttl", "2H"])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 2
