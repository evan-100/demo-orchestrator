"""Engine construction and readiness checks for Crewline."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from demoapp.models import seed_marker

# Only psycopg (v3) is installed; SQLAlchemy maps a bare `postgresql://` to psycopg2.
_POSTGRES_PREFIXES = ("postgresql://", "postgres://")
_PSYCOPG_PREFIX = "postgresql+psycopg://"


def normalize_database_url(database_url: str) -> str:
    """Point plain Postgres URLs at the psycopg v3 driver; leave other schemes alone."""
    for prefix in _POSTGRES_PREFIXES:
        if database_url.startswith(prefix):
            return _PSYCOPG_PREFIX + database_url[len(prefix) :]
    return database_url


def make_engine(database_url: str) -> sa.Engine:
    """Create an engine; `pool_pre_ping` survives Postgres restarts inside the namespace."""
    url = normalize_database_url(database_url)
    connect_args: dict[str, int] = {}
    if url.startswith("postgresql"):
        connect_args["connect_timeout"] = 5  # fail /readyz fast on an unreachable host
    return sa.create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def is_seeded(engine: sa.Engine) -> bool:
    """True only if the database is reachable and a seed marker row exists."""
    try:
        with engine.connect() as conn:
            return conn.execute(sa.select(seed_marker.c.persona).limit(1)).first() is not None
    except SQLAlchemyError:
        return False
