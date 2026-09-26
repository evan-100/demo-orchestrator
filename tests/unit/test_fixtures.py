from datetime import date, timedelta
from pathlib import Path

import pytest

from demoapp import fixtures
from orchestrator.core.personas import load_personas

REPO_PERSONAS = Path(__file__).parents[2] / "personas"
PERSONAS = load_personas(REPO_PERSONAS)
TODAY = date(2026, 9, 23)


@pytest.fixture(params=sorted(PERSONAS))
def persona(request):
    return PERSONAS[request.param]


def test_build_is_deterministic(persona):
    assert fixtures.build(persona, TODAY) == fixtures.build(persona, TODAY)


def test_different_seeds_give_different_people():
    a = fixtures.build(PERSONAS["healthcare"], TODAY)
    b = fixtures.build(PERSONAS["restaurant"], TODAY)
    assert [p.full_name for p in a.people[:5]] != [p.full_name for p in b.people[:5]]


def test_counts_and_roles_match_persona(persona):
    ds = fixtures.build(persona, TODAY)
    assert len(ds.people) == persona.fixtures.employees
    assert len(ds.locations) == persona.fixtures.locations
    assert {p.role for p in ds.people} == set(persona.fixtures.roles)
    location_ids = {loc.id for loc in ds.locations}
    assert {p.location_id for p in ds.people} == location_ids


def test_every_person_has_a_certification_from_the_persona_list(persona):
    ds = fixtures.build(persona, TODAY)
    allowed = set(persona.fixtures.certifications)
    by_person: dict[int, list[str]] = {}
    for cert in ds.certifications:
        by_person.setdefault(cert.person_id, []).append(cert.name)
    for person in ds.people:
        names = by_person.get(person.id, [])
        assert names, f"{person.full_name} has no certification"
        assert set(names) <= allowed


def test_about_ten_percent_of_certifications_expire_within_30_days(persona):
    ds = fixtures.build(persona, TODAY)
    soon = [c for c in ds.certifications if TODAY <= c.expires_on <= TODAY + timedelta(days=30)]
    ratio = len(soon) / len(ds.certifications)
    assert 0.07 <= ratio <= 0.13
    assert all(c.expires_on >= TODAY for c in ds.certifications)


def test_certification_expiry_tracks_today(persona):
    later = TODAY + timedelta(days=100)
    ds = fixtures.build(persona, later)
    soon = [c for c in ds.certifications if later <= c.expires_on <= later + timedelta(days=30)]
    assert soon


def test_shifts_follow_pattern_and_fall_in_current_week(persona):
    ds = fixtures.build(persona, TODAY)
    monday = TODAY - timedelta(days=TODAY.weekday())
    assert ds.shifts
    assert all(monday <= s.day <= monday + timedelta(days=6) for s in ds.shifts)
    expected = {"12h": {720}, "8h": {480}, "split": {240, 360}}[persona.fixtures.shift_pattern]
    assert {s.minutes for s in ds.shifts} == expected
    people = {p.id: p for p in ds.people}
    assert all(people[s.person_id].location_id == s.location_id for s in ds.shifts)
