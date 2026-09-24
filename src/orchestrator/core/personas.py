"""Persona model and loader.

A persona is a YAML file (`personas/<name>/persona.yaml`) describing a
demo vertical: branding, TTL limits, resource quota, and the seed data
shape used by the demo app's fixture builder.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator

from orchestrator.constants import HARD_MAX_TTL_SECONDS
from orchestrator.core.durations import parse_duration

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class Brand(BaseModel):
    company_name: str
    primary_color: str

    @field_validator("primary_color")
    @classmethod
    def _validate_hex_color(cls, value: str) -> str:
        if not _HEX_COLOR.fullmatch(value):
            raise ValueError(f"primary_color {value!r} must be a #RRGGBB hex color")
        return value


class Resources(BaseModel):
    cpu: str
    memory: str
    pods: int


class Fixtures(BaseModel):
    seed: int
    locations: int
    employees: int
    roles: list[str]
    certifications: list[str]
    shift_pattern: Literal["12h", "8h", "split"]


class Persona(BaseModel):
    name: str
    display_name: str
    brand: Brand
    default_ttl: timedelta
    max_ttl: timedelta
    resources: Resources
    fixtures: Fixtures

    @field_validator("default_ttl", "max_ttl", mode="before")
    @classmethod
    def _parse_ttl(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_duration(value)
        return value

    @model_validator(mode="after")
    def _validate_ttl_bounds(self) -> Persona:
        hard_max = timedelta(seconds=HARD_MAX_TTL_SECONDS)
        if self.max_ttl > hard_max:
            raise ValueError(f"persona {self.name!r}: max_ttl exceeds the hard ceiling of 8h")
        if self.default_ttl > self.max_ttl:
            raise ValueError(f"persona {self.name!r}: default_ttl must be <= max_ttl")
        return self


class UnknownPersonaError(KeyError):
    """Raised when a requested persona name isn't among the loaded personas."""


def load_persona_file(path: Path) -> Persona:
    """Load and validate a single persona YAML file (e.g. a mounted ConfigMap key)."""
    return Persona.model_validate(yaml.safe_load(path.read_text()))


def load_personas(directory: Path) -> dict[str, Persona]:
    """Load every `<directory>/<name>/persona.yaml` into a `{name: Persona}` map.

    Raises `ValueError` (via pydantic validation) if a persona's `name` field
    doesn't match its folder name, or its TTLs are invalid.
    """
    personas: dict[str, Persona] = {}
    for path in sorted(directory.glob("*/persona.yaml")):
        folder_name = path.parent.name
        persona = load_persona_file(path)
        if persona.name != folder_name:
            raise ValueError(
                f"persona name {persona.name!r} does not match folder name {folder_name!r}"
            )
        personas[persona.name] = persona
    return personas


def get_persona(personas: dict[str, Persona], name: str) -> Persona:
    """Look up a persona by name, raising `UnknownPersonaError` listing valid names."""
    try:
        return personas[name]
    except KeyError:
        valid = ", ".join(sorted(personas))
        raise UnknownPersonaError(f"unknown persona {name!r}: valid personas are {valid}") from None
