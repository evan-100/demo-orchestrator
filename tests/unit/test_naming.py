import random
import re

import pytest

from orchestrator.core.naming import admissible_namespace_for, generate_env_name, namespace_for


def test_generated_names_are_dns_safe_and_deterministic_with_rng():
    a = generate_env_name("healthcare", random.Random(1))
    b = generate_env_name("healthcare", random.Random(1))
    assert a == b and re.fullmatch(r"healthcare-[a-z0-9]{4}", a)


def test_namespace_for_rejects_too_long():
    with pytest.raises(ValueError):
        namespace_for("x" * 60)


def test_admissible_namespace_for_rejects_protected_install_namespace():
    with pytest.raises(ValueError, match="demo-orchestrator.*protected"):
        admissible_namespace_for("orchestrator")


def test_admissible_namespace_for_keeps_namespace_for_checks():
    assert admissible_namespace_for("healthcare-ab12") == "demo-healthcare-ab12"
    with pytest.raises(ValueError):
        admissible_namespace_for("x" * 60)
    with pytest.raises(ValueError):
        admissible_namespace_for("Bad_Name")
