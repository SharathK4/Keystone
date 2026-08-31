"""Shared base types, identifier helpers and the project time convention.

Time convention
---------------
Every quantity in the mathematical core uses **simulation time in hours**, a
float measured from a run's ``epoch`` (an absolute UTC datetime recorded once
per dataset). Wall-clock datetimes appear only at the persistence and API
boundaries, converted through :func:`to_sim_time` / :func:`to_wall_clock`.

Rationale: the propagation math involves delay distributions, exponential
kernels and integrals over time. Doing that arithmetic on ``datetime`` objects
is error-prone; doing it on floats with a single documented unit is not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- identifier types -------------------------------------------------------

MerchantId = str
ObligationId = str
EventId = str
RunId = str

HOURS_PER_DAY = 24.0
SECONDS_PER_HOUR = 3600.0

# Amounts are held in **paise** (integer minor units) at the persistence layer
# and as floats in the math core. Money is never compared with `==` on floats;
# use `AMOUNT_TOL` for equality checks.
AMOUNT_TOL = 1e-6

Amount = Annotated[float, Field(description="Cash amount in INR (major units).")]
SimTime = Annotated[float, Field(description="Simulation time in hours since dataset epoch.")]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]


def new_id(prefix: str) -> str:
    """Readable, collision-free identifier, e.g. ``mrc_9f2c1a...``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def to_sim_time(moment: datetime, epoch: datetime) -> float:
    """Convert an absolute datetime to simulation hours since ``epoch``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)
    return (moment - epoch).total_seconds() / SECONDS_PER_HOUR


def to_wall_clock(t: float, epoch: datetime) -> datetime:
    """Convert simulation hours back to an absolute UTC datetime."""
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)
    return epoch + timedelta(hours=t)


class DomainModel(BaseModel):
    """Base for every domain value object.

    Frozen by default: domain objects are values, and the simulator advances
    state by producing new objects rather than mutating shared ones. Mutable
    simulator state lives in dedicated dataclasses under ``lce.simulation``.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        ser_json_inf_nan="constants",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data: Any) -> Any:
        """Allow a serialised model to be validated straight back in.

        ``model_dump()`` includes ``@computed_field`` values, but they are
        derived rather than stored, so ``extra="forbid"`` would reject the very
        dict the model just produced. Stripping them here keeps round-trips
        working - which the graph payload, the API and the repositories all rely
        on - while still rejecting genuinely unknown keys.
        """
        if isinstance(data, dict) and cls.model_computed_fields:
            computed = set(cls.model_computed_fields)
            if computed & data.keys():
                return {k: v for k, v in data.items() if k not in computed}
        return data

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict (enums as values, datetimes as ISO strings)."""
        return self.model_dump(mode="json")
