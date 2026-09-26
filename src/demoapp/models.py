"""Crewline database schema (SQLAlchemy Core, portable across Postgres and SQLite)."""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

locations = sa.Table(
    "locations",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("address", sa.String(200), nullable=False),
    sa.Column("city", sa.String(120), nullable=False),
    sa.Column("phone", sa.String(40), nullable=False),
)

people = sa.Table(
    "people",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("first_name", sa.String(80), nullable=False),
    sa.Column("last_name", sa.String(80), nullable=False),
    sa.Column("email", sa.String(200), nullable=False),
    sa.Column("role", sa.String(120), nullable=False),
    sa.Column("location_id", sa.ForeignKey("locations.id"), nullable=False),
    sa.Column("employment_type", sa.String(20), nullable=False),
    sa.Column("hourly_rate_cents", sa.Integer, nullable=False),
    sa.Column("hired_on", sa.Date, nullable=False),
)

shifts = sa.Table(
    "shifts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("person_id", sa.ForeignKey("people.id"), nullable=False),
    sa.Column("location_id", sa.ForeignKey("locations.id"), nullable=False),
    sa.Column("day", sa.Date, nullable=False, index=True),
    sa.Column("label", sa.String(40), nullable=False),
    sa.Column("start_minute", sa.Integer, nullable=False),
    sa.Column("minutes", sa.Integer, nullable=False),
)

certifications = sa.Table(
    "certifications",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("person_id", sa.ForeignKey("people.id"), nullable=False),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("issued_on", sa.Date, nullable=False),
    sa.Column("expires_on", sa.Date, nullable=False, index=True),
)

seed_marker = sa.Table(
    "seed_marker",
    metadata,
    sa.Column("persona", sa.String(80), primary_key=True),
    sa.Column("seeded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("row_counts", sa.JSON, nullable=False),
)
