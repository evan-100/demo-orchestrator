"""Seed a Crewline database from a persona file.

Usage: `python -m demoapp.seed --persona-file /persona/persona.yaml` with
`DATABASE_URL` in the environment. Creates tables, inserts the persona's
dataset in one transaction and records a `seed_marker` row. Re-running
against a seeded database is a no-op that exits 0.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from demoapp import fixtures, models
from demoapp.db import make_engine
from orchestrator.core.expiry import utcnow
from orchestrator.core.personas import Persona, load_persona_file

log = logging.getLogger("demoapp.seed")


def _person_row(person: fixtures.Person) -> dict[str, Any]:
    row = asdict(person)
    row["hourly_rate_cents"] = round(row.pop("hourly_rate") * 100)
    return row


def run(database_url: str, persona: Persona, today: date | None = None) -> bool:
    """Seed `persona` into `database_url`. Returns False if it was already seeded."""
    dataset = fixtures.build(persona, today or utcnow().date())
    engine = make_engine(database_url)
    try:
        models.metadata.create_all(engine)
        with engine.begin() as conn:
            if conn.execute(sa.select(models.seed_marker.c.persona).limit(1)).first():
                log.info("database already seeded; nothing to do")
                return False
            batches: list[tuple[sa.Table, list[dict[str, Any]]]] = [
                (models.locations, [asdict(x) for x in dataset.locations]),
                (models.people, [_person_row(x) for x in dataset.people]),
                (models.shifts, [asdict(x) for x in dataset.shifts]),
                (models.certifications, [asdict(x) for x in dataset.certifications]),
            ]
            for table, rows in batches:
                conn.execute(table.insert(), rows)
            conn.execute(
                models.seed_marker.insert(),
                {
                    "persona": persona.name,
                    "seeded_at": utcnow(),
                    "row_counts": {table.name: len(rows) for table, rows in batches},
                },
            )
        log.info("seeded persona %s", persona.name)
        return True
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demoapp.seed", description=__doc__)
    parser.add_argument("--persona-file", type=Path, required=True)
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL must be set")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run(database_url, load_persona_file(args.persona_file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
