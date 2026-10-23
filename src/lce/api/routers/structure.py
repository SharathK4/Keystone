"""Read-only structural results: the value of speed.

Served from a verified, content-addressed artifact built offline. Nothing here
computes; no database is needed.
"""

from __future__ import annotations

from fastapi import APIRouter

from lce.structure.models import ProofView, PullRecord, PullsView, SpeedScenarioView, SpeedSummary
from lce.structure.store import get_proof_store, get_pulls_store, get_speed_store

router = APIRouter(prefix="/structure", tags=["structure"])


@router.get("/speed", response_model=SpeedSummary)
def speed_summary() -> SpeedSummary:
    """How much of a plan's effect survives when the money lands later."""
    return get_speed_store().summary()


@router.get("/speed/scenarios/{scenario_id}", response_model=SpeedScenarioView)
def speed_for_scenario(scenario_id: str) -> SpeedScenarioView:
    """Every delay for one scenario: the frozen plan and the replanned one."""
    return get_speed_store().scenario(scenario_id)


@router.get("/pulls", response_model=PullsView)
def pulls() -> PullsView:
    """Every merchant's failure, analysed offline at 1.5x, 2x and 2.5x its slack."""
    return get_pulls_store().view


@router.get("/pulls/{merchant_id}", response_model=PullRecord)
def pull(merchant_id: str) -> PullRecord:
    """One merchant's failure: who else is constrained, when, and the plan."""
    return get_pulls_store().record(merchant_id)


@router.get("/proof", response_model=ProofView)
def proof() -> ProofView:
    """The inspection certificate: barrier, recovery, audit, results and weaknesses."""
    return get_proof_store().view
