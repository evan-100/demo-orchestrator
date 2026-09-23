"""Environment name generation and namespace derivation."""

from __future__ import annotations

import random
import re
import string

from orchestrator.constants import NS_PREFIX

_SUFFIX_ALPHABET = string.digits + "abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LENGTH = 4
_DNS_1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_LABEL_LENGTH = 63


def generate_env_name(persona: str, rng: random.Random | None = None) -> str:
    """Generate an environment name: "<persona>-<4 random [a-z0-9] chars>".

    Deterministic when given a seeded `random.Random`; draws from a fresh
    generator otherwise.
    """
    generator = rng if rng is not None else random.Random()
    suffix = "".join(generator.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
    return f"{persona}-{suffix}"


def namespace_for(env_name: str) -> str:
    """Derive the Kubernetes namespace name for an environment name.

    Raises `ValueError` if the result exceeds 63 characters or is not a
    valid DNS-1123 label.
    """
    namespace = f"{NS_PREFIX}{env_name}"
    if len(namespace) > _MAX_LABEL_LENGTH:
        raise ValueError(
            f"namespace {namespace!r} exceeds the {_MAX_LABEL_LENGTH}-character DNS-1123 limit"
        )
    if not _DNS_1123_LABEL.fullmatch(namespace):
        raise ValueError(f"namespace {namespace!r} is not a valid DNS-1123 label")
    return namespace
