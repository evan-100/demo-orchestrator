"""Crewline: the sample workforce app shown inside every demo environment.

Configuration comes from the environment, read when `create_app()` runs:
`DATABASE_URL`, `PERSONA_FILE` (mounted persona.yaml) and `EXPIRES_AT`
(RFC3339, injected by the operator). Importing this module never touches the
database, so a missing `DATABASE_URL` only fails requests that need it.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from demoapp.db import is_seeded, make_engine
from demoapp.fixtures import EXPIRING_WINDOW_DAYS
from demoapp.models import certifications, locations, people, shifts
from orchestrator.core.expiry import from_rfc3339, utcnow
from orchestrator.core.personas import Persona, load_persona_file

_HERE = Path(__file__).parent
_DAY_MINUTES = 24 * 60

NAV = [
    ("dashboard", "/", "Dashboard"),
    ("people", "/people", "People"),
    ("locations", "/locations", "Locations"),
    ("shifts", "/shifts", "Shifts"),
    ("certifications", "/certifications", "Certifications"),
    ("payroll", "/payroll", "Payroll"),
]


def expiry_phrase(expires_at: datetime | None, now: datetime) -> str:
    """Banner phrase such as `expires in 1h 42m`, `expired`, or `no expiry set`."""
    if expires_at is None:
        return "no expiry set"
    seconds = int((expires_at - now).total_seconds())
    if seconds <= 0:
        return "expired"
    hours, minutes = divmod(max(1, seconds // 60), 60)
    return f"expires in {hours}h {minutes}m" if hours else f"expires in {minutes}m"


def _money(cents: float) -> str:
    return f"${cents / 100:,.0f}"


def _clock(minute: int) -> str:
    return f"{(minute // 60) % 24:02d}:{minute % 60:02d}"


def _initials(name: str) -> str:
    return "".join(word[0] for word in name.split()[:2]).upper()


@dataclass
class Settings:
    database_url: str | None
    persona_file: str | None
    expires_at: str | None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            persona_file=os.environ.get("PERSONA_FILE"),
            expires_at=os.environ.get("EXPIRES_AT"),
        )


class Runtime:
    """Lazily built engine and persona shared by every request."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @cached_property
    def engine(self) -> sa.Engine:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        return make_engine(self.settings.database_url)

    @cached_property
    def persona(self) -> Persona:
        if not self.settings.persona_file:
            raise RuntimeError("PERSONA_FILE is not set")
        return load_persona_file(Path(self.settings.persona_file))

    @cached_property
    def expires_at(self) -> datetime | None:
        try:
            return from_rfc3339(self.settings.expires_at) if self.settings.expires_at else None
        except ValueError:
            return None

    def dispose(self) -> None:
        if "engine" in self.__dict__:
            self.engine.dispose()


def _week(today: date) -> list[date]:
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def _dashboard(conn: sa.Connection, today: date) -> dict[str, Any]:
    week = _week(today)
    soon = today + timedelta(days=EXPIRING_WINDOW_DAYS)
    locs = conn.execute(sa.select(locations).order_by(locations.c.name)).all()
    week_shifts = conn.execute(
        sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(shifts.c.minutes), 0)).where(
            shifts.c.day.between(week[0], week[-1])
        )
    ).one()
    # Shift board: per location, one bar per shift type with its headcount. Overnight
    # shifts that started yesterday show as a carry-over bar from midnight.
    yesterday = today - timedelta(days=1)
    board_rows = conn.execute(
        sa.select(
            shifts.c.day,
            shifts.c.location_id,
            shifts.c.label,
            shifts.c.start_minute,
            shifts.c.minutes,
            sa.func.count().label("staff"),
        )
        .where(shifts.c.day.in_([yesterday, today]))
        .group_by(
            shifts.c.day,
            shifts.c.location_id,
            shifts.c.label,
            shifts.c.start_minute,
            shifts.c.minutes,
        )
        .order_by(shifts.c.start_minute)
    ).all()
    bars: dict[int, list[dict[str, Any]]] = defaultdict(list)
    on_shift_today = 0
    for r in board_rows:
        end = r.start_minute + r.minutes
        if r.day == today:
            on_shift_today += r.staff
            start, stop = r.start_minute, min(end, _DAY_MINUTES)
        elif end > _DAY_MINUTES:
            start, stop = 0, end - _DAY_MINUTES
        else:
            continue
        bars[r.location_id].append(
            {
                "label": r.label,
                "staff": r.staff,
                "left": start / _DAY_MINUTES * 100,
                "width": (stop - start) / _DAY_MINUTES * 100,
                "range": f"{_clock(r.start_minute)}–{_clock(end)}",
                "continues": r.day == today and end > _DAY_MINUTES,
                "carried": r.day == yesterday,
            }
        )
    for location_bars in bars.values():
        location_bars.sort(key=lambda b: b["left"])
    board = [{"location": loc, "bars": bars.get(loc.id, [])} for loc in locs]
    expiring = conn.execute(
        sa.select(certifications, people.c.first_name, people.c.last_name, people.c.role)
        .join(people, people.c.id == certifications.c.person_id)
        .where(certifications.c.expires_on.between(today, soon))
        .order_by(certifications.c.expires_on, certifications.c.id)
    ).all()
    roles = conn.execute(
        sa.select(people.c.role, sa.func.count().label("n"))
        .group_by(people.c.role)
        .order_by(sa.func.count().desc(), people.c.role)
    ).all()
    headcount = sum(r.n for r in roles)
    return {
        "headcount": headcount,
        "location_count": len(locs),
        "on_shift_today": on_shift_today,
        "week_shift_count": week_shifts[0],
        "week_hours": week_shifts[1] / 60,
        "expiring": expiring[:6],
        "expiring_count": len(expiring),
        "board": board,
        "has_carried": any(b["carried"] for bs in bars.values() for b in bs),
        "roles": roles,
        "role_max": max((r.n for r in roles), default=1),
        "hours_ticks": [0, 6, 12, 18, 24],
    }


def _people_page(conn: sa.Connection, location_id: int | None, today: date) -> dict[str, Any]:
    locs = conn.execute(sa.select(locations).order_by(locations.c.name)).all()
    next_expiry = (
        sa.select(
            certifications.c.person_id,
            sa.func.min(certifications.c.expires_on).label("next_expiry"),
            sa.func.count().label("cert_count"),
        )
        .group_by(certifications.c.person_id)
        .subquery()
    )
    query = (
        sa.select(
            people,
            locations.c.name.label("location_name"),
            next_expiry.c.next_expiry,
            next_expiry.c.cert_count,
        )
        .join(locations, locations.c.id == people.c.location_id)
        .outerjoin(next_expiry, next_expiry.c.person_id == people.c.id)
        .order_by(people.c.last_name, people.c.first_name)
    )
    if location_id is not None:
        query = query.where(people.c.location_id == location_id)
    rows = conn.execute(query).all()
    soon = today + timedelta(days=EXPIRING_WINDOW_DAYS)
    return {
        "locations": locs,
        "selected_location": location_id,
        "rows": rows,
        "soon": soon,
    }


def _locations_page(conn: sa.Connection, today: date) -> dict[str, Any]:
    week = _week(today)
    soon = today + timedelta(days=EXPIRING_WINDOW_DAYS)
    locs = conn.execute(sa.select(locations).order_by(locations.c.name)).all()
    headcount = dict(
        conn.execute(
            sa.select(people.c.location_id, sa.func.count()).group_by(people.c.location_id)
        )
        .tuples()
        .all()
    )
    today_staff = dict(
        conn.execute(
            sa.select(shifts.c.location_id, sa.func.count(sa.distinct(shifts.c.person_id)))
            .where(shifts.c.day == today)
            .group_by(shifts.c.location_id)
        )
        .tuples()
        .all()
    )
    week_minutes = dict(
        conn.execute(
            sa.select(shifts.c.location_id, sa.func.sum(shifts.c.minutes))
            .where(shifts.c.day.between(week[0], week[-1]))
            .group_by(shifts.c.location_id)
        )
        .tuples()
        .all()
    )
    expiring = dict(
        conn.execute(
            sa.select(people.c.location_id, sa.func.count())
            .join(certifications, certifications.c.person_id == people.c.id)
            .where(certifications.c.expires_on.between(today, soon))
            .group_by(people.c.location_id)
        )
        .tuples()
        .all()
    )
    roles_by_location: dict[int, dict[str, int]] = defaultdict(dict)
    for loc_id, role, n in conn.execute(
        sa.select(people.c.location_id, people.c.role, sa.func.count())
        .group_by(people.c.location_id, people.c.role)
        .order_by(sa.func.count().desc(), people.c.role)
    ).tuples():
        roles_by_location[loc_id][role] = n
    return {
        "rows": [
            {
                "location": loc,
                "headcount": headcount.get(loc.id, 0),
                "on_shift_today": today_staff.get(loc.id, 0),
                "week_hours": (week_minutes.get(loc.id) or 0) / 60,
                "expiring": expiring.get(loc.id, 0),
                "roles": roles_by_location.get(loc.id, {}),
            }
            for loc in locs
        ]
    }


def _shifts_page(conn: sa.Connection, today: date, day: date) -> dict[str, Any]:
    week = _week(today)
    locs = conn.execute(sa.select(locations).order_by(locations.c.name)).all()
    matrix: dict[tuple[int, date], tuple[int, float]] = {}
    for loc_id, d, n, minutes in conn.execute(
        sa.select(
            shifts.c.location_id, shifts.c.day, sa.func.count(), sa.func.sum(shifts.c.minutes)
        )
        .where(shifts.c.day.between(week[0], week[-1]))
        .group_by(shifts.c.location_id, shifts.c.day)
    ).tuples():
        matrix[(loc_id, d)] = (n, minutes / 60)
    roster_rows = conn.execute(
        sa.select(
            shifts,
            people.c.first_name,
            people.c.last_name,
            people.c.role,
            locations.c.name.label("location_name"),
        )
        .join(people, people.c.id == shifts.c.person_id)
        .join(locations, locations.c.id == shifts.c.location_id)
        .where(shifts.c.day == day)
        .order_by(shifts.c.start_minute, locations.c.name, people.c.last_name)
    ).all()
    groups: dict[str, dict[str, Any]] = {}
    for r in roster_rows:
        group = groups.setdefault(
            r.label,
            {
                "label": r.label,
                "range": f"{_clock(r.start_minute)}–{_clock(r.start_minute + r.minutes)}",
                "rows": [],
            },
        )
        group["rows"].append(r)
    return {
        "week": week,
        "day": day,
        "locations": locs,
        "matrix": matrix,
        "groups": list(groups.values()),
        "roster_count": len(roster_rows),
    }


def _certifications_page(conn: sa.Connection, today: date) -> dict[str, Any]:
    soon = today + timedelta(days=EXPIRING_WINDOW_DAYS)
    rows = conn.execute(
        sa.select(
            certifications,
            people.c.first_name,
            people.c.last_name,
            people.c.role,
            locations.c.name.label("location_name"),
        )
        .join(people, people.c.id == certifications.c.person_id)
        .join(locations, locations.c.id == people.c.location_id)
        .order_by(certifications.c.expires_on, certifications.c.id)
    ).all()
    summary: dict[str, dict[str, int]] = {}
    for r in rows:
        entry = summary.setdefault(r.name, {"held": 0, "expiring": 0})
        entry["held"] += 1
        if today <= r.expires_on <= soon:
            entry["expiring"] += 1
    return {
        "expiring": [r for r in rows if r.expires_on <= soon],
        "current": [r for r in rows if r.expires_on > soon],
        "summary": sorted(summary.items()),
    }


def _payroll_page(conn: sa.Connection, today: date) -> dict[str, Any]:
    week = _week(today)
    in_period = shifts.c.day.between(week[0], week[-1])
    cost = sa.func.sum(shifts.c.minutes * people.c.hourly_rate_cents)
    minutes = sa.func.sum(shifts.c.minutes)
    base = sa.select().select_from(shifts.join(people, people.c.id == shifts.c.person_id))
    by_location = conn.execute(
        base.add_columns(
            locations.c.name,
            sa.func.count(sa.distinct(people.c.id)).label("staff"),
            minutes.label("minutes"),
            cost.label("cost"),
        )
        .join(locations, locations.c.id == shifts.c.location_id)
        .where(in_period)
        .group_by(locations.c.name)
        .order_by(locations.c.name)
    ).all()
    by_role = conn.execute(
        base.add_columns(
            people.c.role,
            sa.func.count(sa.distinct(people.c.id)).label("staff"),
            minutes.label("minutes"),
            cost.label("cost"),
        )
        .where(in_period)
        .group_by(people.c.role)
        .order_by(cost.desc(), people.c.role)
    ).all()
    total_cost = sum(r.cost for r in by_location)  # cents × minutes
    total_minutes = sum(r.minutes for r in by_location)
    staff = conn.execute(
        base.add_columns(sa.func.count(sa.distinct(people.c.id))).where(in_period)
    ).scalar_one()

    def shape(rows: Sequence[sa.Row[Any]], key: str) -> list[dict[str, Any]]:
        return [
            {
                "name": getattr(r, key),
                "staff": r.staff,
                "hours": r.minutes / 60,
                "gross": _money(r.cost / 60),
                "avg_rate": f"${r.cost / r.minutes / 100:,.2f}",
                "share": r.cost / total_cost * 100 if total_cost else 0,
            }
            for r in rows
        ]

    return {
        "period_start": week[0],
        "period_end": week[-1],
        "gross": _money(total_cost / 60),
        "hours": total_minutes / 60,
        "staff": staff,
        "avg_rate": f"${total_cost / total_minutes / 100:,.2f}" if total_minutes else "$0.00",
        "by_location": shape(by_location, "name"),
        "by_role": shape(by_role, "role"),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime = Runtime(settings or Settings.from_env())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        runtime.dispose()

    app = FastAPI(title="Crewline", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["money"] = _money
    templates.env.filters["clock"] = _clock

    def render(request: Request, page: str, title: str, **context: Any) -> HTMLResponse:
        persona = runtime.persona
        now = utcnow()
        return templates.TemplateResponse(
            request,
            f"{page}.html",
            {
                "page": page,
                "title": title,
                "nav": NAV,
                "persona": persona,
                "brand": persona.brand,
                "initials": _initials(persona.brand.company_name),
                "expiry": expiry_phrase(runtime.expires_at, now),
                "expires_at": runtime.expires_at,
                "today": now.date(),
                "window_days": EXPIRING_WINDOW_DAYS,
                **context,
            },
        )

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse)
    def readyz() -> PlainTextResponse:
        try:
            ready = is_seeded(runtime.engine)
        except (RuntimeError, ImportError, SQLAlchemyError):
            # Unset DATABASE_URL, missing DB driver, or malformed URL (ArgumentError is a
            # SQLAlchemyError): report "not ready" instead of a 500.
            ready = False
        if ready:
            return PlainTextResponse("ready")
        return PlainTextResponse("not ready: database unreachable or not seeded", status_code=503)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        today = utcnow().date()
        with runtime.engine.connect() as conn:
            data = _dashboard(conn, today)
        return render(request, "dashboard", "Dashboard", **data)

    @app.get("/people", response_class=HTMLResponse)
    def people_page(request: Request, location: int | None = None) -> HTMLResponse:
        with runtime.engine.connect() as conn:
            data = _people_page(conn, location, utcnow().date())
        return render(request, "people", "People", **data)

    @app.get("/locations", response_class=HTMLResponse)
    def locations_page(request: Request) -> HTMLResponse:
        with runtime.engine.connect() as conn:
            data = _locations_page(conn, utcnow().date())
        return render(request, "locations", "Locations", **data)

    @app.get("/shifts", response_class=HTMLResponse)
    def shifts_page(request: Request, day: date | None = None) -> HTMLResponse:
        today = utcnow().date()
        with runtime.engine.connect() as conn:
            data = _shifts_page(conn, today, day or today)
        return render(request, "shifts", "Shifts", **data)

    @app.get("/certifications", response_class=HTMLResponse)
    def certifications_page(request: Request) -> HTMLResponse:
        with runtime.engine.connect() as conn:
            data = _certifications_page(conn, utcnow().date())
        return render(request, "certifications", "Certifications", **data)

    @app.get("/payroll", response_class=HTMLResponse)
    def payroll_page(request: Request) -> HTMLResponse:
        with runtime.engine.connect() as conn:
            data = _payroll_page(conn, utcnow().date())
        return render(request, "payroll", "Payroll", **data)

    return app


app = create_app()
