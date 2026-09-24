from pathlib import Path

import pytest

from orchestrator.core.personas import (
    UnknownPersonaError,
    get_persona,
    load_persona_file,
    load_personas,
)

REPO_PERSONAS = Path(__file__).parents[2] / "personas"


def test_ships_three_valid_personas():
    assert set(load_personas(REPO_PERSONAS)) == {"healthcare", "manufacturing", "restaurant"}


def test_unknown_persona_lists_valid_names():
    with pytest.raises(UnknownPersonaError, match="healthcare, manufacturing, restaurant"):
        get_persona(load_personas(REPO_PERSONAS), "retail")


def test_default_ttl_above_max_rejected(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    src = (REPO_PERSONAS / "healthcare" / "persona.yaml").read_text()
    (d / "persona.yaml").write_text(
        src.replace("name: healthcare", "name: bad").replace("default_ttl: 2h", "default_ttl: 9h")
    )
    with pytest.raises(ValueError):
        load_personas(tmp_path)


def test_load_persona_file_matches_directory_loader():
    persona = load_persona_file(REPO_PERSONAS / "restaurant" / "persona.yaml")
    assert persona == load_personas(REPO_PERSONAS)["restaurant"]
