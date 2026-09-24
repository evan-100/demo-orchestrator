from datetime import date, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from demoapp import models, seed
from demoapp.app import create_app
from orchestrator.core.expiry import to_rfc3339, utcnow
from orchestrator.core.personas import load_personas

REPO_PERSONAS = Path(__file__).parents[2] / "personas"
PERSONA_FILE = REPO_PERSONAS / "healthcare" / "persona.yaml"
PERSONA = load_personas(REPO_PERSONAS)["healthcare"]
PAGES = ["/", "/people", "/locations", "/shifts", "/certifications", "/payroll"]


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/crewline.db"


@pytest.fixture
def client(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("PERSONA_FILE", str(PERSONA_FILE))
    monkeypatch.setenv("EXPIRES_AT", to_rfc3339(utcnow() + timedelta(hours=1, minutes=42)))
    with TestClient(create_app()) as c:
        yield c


def _count(db_url: str, table: sa.Table) -> int:
    engine = sa.create_engine(db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
    finally:
        engine.dispose()


def test_healthz_always_ok(client):
    assert client.get("/healthz").status_code == 200


def test_readyz_503_before_seed_and_200_after(client, db_url):
    assert client.get("/readyz").status_code == 503
    seed.run(db_url, PERSONA)
    assert client.get("/readyz").status_code == 200


def test_readyz_503_when_database_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/missing-dir/x.db")
    monkeypatch.setenv("PERSONA_FILE", str(PERSONA_FILE))
    with TestClient(create_app()) as c:
        assert c.get("/readyz").status_code == 503


@pytest.mark.parametrize("url", ["mysql+mysqldb://x/y", "not a url"])
def test_readyz_503_when_engine_cannot_be_created(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PERSONA_FILE", str(PERSONA_FILE))
    with TestClient(create_app()) as c:
        assert c.get("/readyz").status_code == 503


def test_readyz_503_when_database_url_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PERSONA_FILE", str(PERSONA_FILE))
    with TestClient(create_app()) as c:
        assert c.get("/readyz").status_code == 503


def test_seed_is_idempotent(db_url):
    seed.run(db_url, PERSONA, today=date(2026, 9, 23))
    counts = {t.name: _count(db_url, t) for t in models.metadata.sorted_tables}
    seed.run(db_url, PERSONA, today=date(2026, 9, 23))
    assert {t.name: _count(db_url, t) for t in models.metadata.sorted_tables} == counts
    assert counts["people"] == PERSONA.fixtures.employees
    assert counts["seed_marker"] == 1


def test_seed_cli_reads_env(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    assert seed.main(["--persona-file", str(PERSONA_FILE)]) == 0
    assert _count(db_url, models.people) == PERSONA.fixtures.employees


def test_people_page_shows_brand(client, db_url):
    seed.run(db_url, PERSONA)
    body = client.get("/people").text
    assert "Riverbend Clinics" in body
    assert PERSONA.brand.primary_color in body


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_with_demo_banner(client, db_url, path):
    seed.run(db_url, PERSONA)
    resp = client.get(path)
    assert resp.status_code == 200
    assert "Demo environment · Healthcare — Riverbend Clinics · expires in 1h 4" in resp.text


def test_banner_without_expiry(client, db_url, monkeypatch):
    seed.run(db_url, PERSONA)
    monkeypatch.delenv("EXPIRES_AT")
    with TestClient(create_app()) as c:
        assert "Healthcare — Riverbend Clinics · no expiry set" in c.get("/").text


def test_certifications_page_flags_expiring_soon(client, db_url):
    seed.run(db_url, PERSONA)
    assert "Expiring within 30 days" in client.get("/certifications").text
