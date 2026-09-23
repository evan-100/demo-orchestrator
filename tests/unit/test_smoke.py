"""Smoke test: the package imports and toolchain is wired up correctly."""

import orchestrator.constants


def test_ns_prefix() -> None:
    assert orchestrator.constants.NS_PREFIX == "demo-"
