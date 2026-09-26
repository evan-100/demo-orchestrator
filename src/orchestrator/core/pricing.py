"""On-demand pricing loaded from `pricing.yaml` (spec A7 cost model).

Prices are controller-sourced GKE Autopilot list prices for one region, with
their source URL and retrieval date recorded in the file itself. Nothing here
computes or guesses a price; `load_pricing` only validates the file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel


class Pricing(BaseModel):
    vcpu_hour_usd: float
    gib_hour_usd: float
    source_url: str
    retrieved: date
    region: str = ""


def load_pricing(path: Path) -> Pricing:
    """Load and validate a pricing file (e.g. `pricing.yaml`) into a `Pricing`."""
    return Pricing.model_validate(yaml.safe_load(path.read_text()))
