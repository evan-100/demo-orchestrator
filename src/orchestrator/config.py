"""Runtime settings for the Demo Environment Orchestrator.

Values are read from environment variables (no prefix) with sensible
local defaults, so the operator, sweeper, and CLI all agree on where
the ledger and personas live and what the operational limits are.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration, sourced from the environment."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    ledger_path: Path = Path("./data/ledger.jsonl")
    personas_dir: Path = Path("./personas")
    max_concurrent_envs: int = 5
    provision_timeout: int = 300
    sweep_grace: int = 120
    base_domain: str = "demo.localtest.me"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings`, constructed once and cached."""
    return Settings()
