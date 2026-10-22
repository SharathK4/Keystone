"""Read-only structural results: the value of speed.

Served from a verified, content-addressed artifact built offline. Nothing here
computes; no database is needed.
"""

from __future__ import annotations

from fastapi import APIRouter

from lce.structure.models import SpeedScenarioView, SpeedSummary
from lce.structure.store import get_speed_store

router = APIRouter(prefix="/structure", tags=["structure"])


@router.get("/speed", response_model=SpeedSummary)
def speed_summary() -> SpeedSummary:
    """How much of a plan's effect survives when the money lands later."""
    return get_speed_store().summary()


@router.get("/speed/scenarios/{scenario_id}", response_model=SpeedScenarioView)
def speed_for_scenario(scenario_id: str) -> SpeedScenarioView:
    """Every delay for one scenario: the frozen plan and the replanned one."""
    return get_speed_store().scenario(scenario_id)
