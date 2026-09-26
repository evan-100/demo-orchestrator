"""Deterministic Crewline dataset built from a persona's `fixtures` block.

`build(persona, today)` always returns the same people, locations and shifts
for a given `fixtures.seed`. Dates (the shift week, certification expiry) are
anchored to `today` so "expiring soon" is true on the day a demo runs, while a
fixed `today` gives byte-identical output for tests and screenshots.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, timedelta

from faker import Faker

from orchestrator.core.personas import Persona

EXPIRING_WINDOW_DAYS = 30
_EXPIRING_SHARE = 0.10

# Typical US hourly base rates by role keyword (first match wins). These only
# make the sample payroll look plausible; they are not market data.
_ROLE_RATES: list[tuple[str, float]] = [
    ("physician", 112.0),
    ("registered nurse", 47.0),
    ("general manager", 36.0),
    ("supervisor", 34.0),
    ("maintenance", 33.0),
    ("lab tech", 29.0),
    ("inspector", 27.0),
    ("forklift", 23.0),
    ("operator", 24.0),
    ("medical assistant", 22.0),
    ("line cook", 19.0),
    ("front desk", 19.0),
    ("bartender", 16.0),
    ("host", 15.0),
    ("server", 14.0),
]
_DEFAULT_RATE = 22.0
_SENIOR_ROLE = re.compile(r"physician|manager|supervisor", re.IGNORECASE)

# Plausibility hints: a certification whose name contains the key is only held
# by roles containing one of the listed keywords. Unlisted certifications (or
# ones no persona role matches) are open to every role.
_CERT_ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "rn license": ("nurse",),
    "acls": ("nurse", "physician"),
    "forklift": ("forklift", "supervisor"),
    "alcohol": ("bartender", "server", "manager"),
}

# Site names and metro areas for generated locations (Faker's city names read as fake).
_SITE_NAMES = [
    "Riverside",
    "Oak Park",
    "Westgate",
    "Millbrook",
    "Harbor Point",
    "Cedar Hills",
    "Northfield",
    "Lakeview",
    "Brookside",
    "Eastbridge",
    "Fairmont",
    "Glenwood",
    "Southport",
    "Maple Grove",
    "Stonebridge",
    "Hillcrest",
    "Bayview",
    "Kingsley",
]
_METROS = [
    ("Columbus", "OH"),
    ("Portland", "OR"),
    ("Raleigh", "NC"),
    ("Minneapolis", "MN"),
    ("Denver", "CO"),
    ("Richmond", "VA"),
    ("Madison", "WI"),
    ("Sacramento", "CA"),
    ("Tucson", "AZ"),
    ("Omaha", "NE"),
    ("Albany", "NY"),
    ("Boise", "ID"),
]

# (label, start minute after midnight, duration in minutes) per shift pattern.
_SHIFT_TEMPLATES: dict[str, list[tuple[str, int, int]]] = {
    "12h": [("Day", 7 * 60, 720), ("Night", 19 * 60, 720)],
    "8h": [("Early", 6 * 60, 480), ("Late", 14 * 60, 480), ("Night", 22 * 60, 480)],
    "split": [("Lunch", 10 * 60 + 30, 240), ("Dinner", 16 * 60 + 30, 360)],
}
_SHIFTS_PER_WEEK = {"12h": 3, "8h": 5, "split": 5}


@dataclass(frozen=True)
class Location:
    id: int
    name: str
    address: str
    city: str
    phone: str


@dataclass(frozen=True)
class Person:
    id: int
    first_name: str
    last_name: str
    email: str
    role: str
    location_id: int
    employment_type: str
    hourly_rate: float
    hired_on: date

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class Shift:
    id: int
    person_id: int
    location_id: int
    day: date
    label: str
    start_minute: int
    minutes: int


@dataclass(frozen=True)
class Certification:
    id: int
    person_id: int
    name: str
    issued_on: date
    expires_on: date


@dataclass(frozen=True)
class Dataset:
    locations: list[Location]
    people: list[Person]
    shifts: list[Shift]
    certifications: list[Certification]


def role_rate(role: str) -> float:
    lowered = role.lower()
    for keyword, rate in _ROLE_RATES:
        if keyword in lowered:
            return rate
    return _DEFAULT_RATE


def _email_domain(company_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", company_name.lower()) + ".example"


def _build_locations(fake: Faker, rng: random.Random, count: int) -> list[Location]:
    if count > len(_SITE_NAMES):
        raise ValueError(f"at most {len(_SITE_NAMES)} locations are supported, got {count}")
    city, state = rng.choice(_METROS)  # one metro area per company keeps it believable
    return [
        Location(
            id=i,
            name=name,
            address=fake.street_address(),
            city=f"{city}, {state}",
            phone=fake.numerify("(###) 555-####"),
        )
        for i, name in enumerate(rng.sample(_SITE_NAMES, count), start=1)
    ]


def _eligible_certifications(role: str, names: list[str], roles: list[str]) -> list[str]:
    eligible = []
    for name in names:
        hint = next((v for k, v in _CERT_ROLE_HINTS.items() if k in name.lower()), None)
        hinted_roles = [r for r in roles if hint and any(h in r.lower() for h in hint)]
        if not hinted_roles or role in hinted_roles:
            eligible.append(name)
    return eligible or list(names)


def _build_people(
    persona: Persona, fake: Faker, rng: random.Random, locations: list[Location], today: date
) -> list[Person]:
    roles = persona.fixtures.roles
    # Roles listed first are the most common; senior roles are always rare.
    weights = [1 if _SENIOR_ROLE.search(r) else len(roles) - i + 1 for i, r in enumerate(roles)]
    count = persona.fixtures.employees
    # Every role appears at least once; the rest are weighted towards frontline roles.
    assigned = list(roles) + rng.choices(roles, weights=weights, k=max(0, count - len(roles)))
    rng.shuffle(assigned)
    domain = _email_domain(persona.brand.company_name)
    people = []
    for i, role in enumerate(assigned[:count], start=1):
        first, last = fake.first_name(), fake.last_name()
        rate = round(role_rate(role) * rng.uniform(0.9, 1.15), 2)
        people.append(
            Person(
                id=i,
                first_name=first,
                last_name=last,
                email=f"{first[0].lower()}.{last.lower().replace(' ', '')}{i}@{domain}",
                role=role,
                location_id=locations[(i - 1) % len(locations)].id,
                employment_type="Part-time" if rng.random() < 0.2 else "Full-time",
                hourly_rate=rate,
                hired_on=today - timedelta(days=rng.randint(30, 9 * 365)),
            )
        )
    return people


def _build_shifts(
    persona: Persona, rng: random.Random, people: list[Person], today: date
) -> list[Shift]:
    pattern = persona.fixtures.shift_pattern
    templates = _SHIFT_TEMPLATES[pattern]
    monday = today - timedelta(days=today.weekday())
    shifts: list[Shift] = []
    for person in people:
        per_week = _SHIFTS_PER_WEEK[pattern]
        if person.employment_type == "Part-time":
            per_week = max(2, per_week - 2)
        home = rng.randrange(len(templates))
        for offset in sorted(rng.sample(range(7), per_week)):
            day = monday + timedelta(days=offset)
            picks = [templates[home]]
            if pattern == "split" and rng.random() < 0.25:
                picks = templates  # a "double": lunch and dinner on the same day
            for label, start, minutes in picks:
                shifts.append(
                    Shift(
                        id=len(shifts) + 1,
                        person_id=person.id,
                        location_id=person.location_id,
                        day=day,
                        label=label,
                        start_minute=start,
                        minutes=minutes,
                    )
                )
    return shifts


def _build_certifications(
    persona: Persona, rng: random.Random, people: list[Person], today: date
) -> list[Certification]:
    names = persona.fixtures.certifications
    roles = persona.fixtures.roles
    held: list[tuple[int, str]] = []
    for person in people:
        eligible = _eligible_certifications(person.role, names, roles)
        # Most people hold every credential their role calls for; some are missing one.
        k = max(1, len(eligible) - (1 if rng.random() < 0.3 else 0))
        held.extend((person.id, name) for name in sorted(rng.sample(eligible, k)))
    expiring = set(rng.sample(range(len(held)), max(1, round(len(held) * _EXPIRING_SHARE))))
    certs = []
    for index, (person_id, name) in enumerate(held):
        if index in expiring:
            expires_on = today + timedelta(days=rng.randint(2, EXPIRING_WINDOW_DAYS))
        else:
            expires_on = today + timedelta(days=rng.randint(EXPIRING_WINDOW_DAYS + 15, 720))
        certs.append(
            Certification(
                id=index + 1,
                person_id=person_id,
                name=name,
                issued_on=expires_on - timedelta(days=730),
                expires_on=expires_on,
            )
        )
    return certs


def build(persona: Persona, today: date) -> Dataset:
    """Return the full dataset for `persona`, deterministic for its `fixtures.seed` and `today`."""
    seed = persona.fixtures.seed
    fake = Faker("en_US")
    fake.seed_instance(seed)
    rng = random.Random(seed)
    locations = _build_locations(fake, rng, persona.fixtures.locations)
    people = _build_people(persona, fake, rng, locations, today)
    return Dataset(
        locations=locations,
        people=people,
        shifts=_build_shifts(persona, rng, people, today),
        certifications=_build_certifications(persona, rng, people, today),
    )
