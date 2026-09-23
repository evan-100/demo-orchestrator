import random
import re

import pytest

from orchestrator.core.naming import generate_env_name, namespace_for


def test_generated_names_are_dns_safe_and_deterministic_with_rng():
    a = generate_env_name("healthcare", random.Random(1))
    b = generate_env_name("healthcare", random.Random(1))
    assert a == b and re.fullmatch(r"healthcare-[a-z0-9]{4}", a)


def test_namespace_for_rejects_too_long():
    with pytest.raises(ValueError):
        namespace_for("x" * 60)
