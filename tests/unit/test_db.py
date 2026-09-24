import pytest

import demoapp.db as db
from demoapp.db import make_engine, normalize_database_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@db:5432/crewline", "postgresql+psycopg://u:p@db:5432/crewline"),
        ("postgres://u:p@db/crewline", "postgresql+psycopg://u:p@db/crewline"),
        ("postgresql+psycopg://u@db/c", "postgresql+psycopg://u@db/c"),
        ("postgresql+asyncpg://u@db/c", "postgresql+asyncpg://u@db/c"),
        ("sqlite:///tmp/x.db", "sqlite:///tmp/x.db"),
    ],
)
def test_normalize_database_url(url, expected):
    assert normalize_database_url(url) == expected


def test_plain_postgres_url_builds_a_psycopg3_engine():
    # Without the rewrite this raises ModuleNotFoundError (psycopg2 isn't installed).
    engine = make_engine("postgresql://u:p@db:5432/crewline")
    assert engine.dialect.driver == "psycopg"
    engine.dispose()


def test_connect_timeout_only_for_postgres(monkeypatch):
    captured = {}
    real = db.sa.create_engine

    def spy(url, **kwargs):
        captured[url] = kwargs["connect_args"]
        return real(url, **kwargs)

    monkeypatch.setattr(db.sa, "create_engine", spy)
    make_engine("postgres://u@db/c").dispose()
    make_engine("sqlite:///:memory:").dispose()
    assert captured == {
        "postgresql+psycopg://u@db/c": {"connect_timeout": 5},
        "sqlite:///:memory:": {},
    }
