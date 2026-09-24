"""Engine construction and readiness checks for Crewline."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from demoapp.models import seed_marker


def make_engine(database_url: str) -> sa.Engine:
    """Create an engine; `pool_pre_ping` survives Postgres restarts inside the namespace."""
    return sa.create_engine(database_url, pool_pre_ping=True)


def is_seeded(engine: sa.Engine) -> bool:
    """True only if the database is reachable and a seed marker row exists."""
    try:
        with engine.connect() as conn:
            return conn.execute(sa.select(seed_marker.c.persona).limit(1)).first() is not None
    except SQLAlchemyError:
        return False
