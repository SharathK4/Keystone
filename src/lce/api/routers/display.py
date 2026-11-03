"""How the interface prints money: currency, declared rate, sandbox ratio.

Presentation only. The analysis stays in rupees on the wire; the frontend
converts for display with the rate served here, so the rate is configured in
one place and never typed into the client. Nothing here needs a database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, ValidationError

from lce.config import get_display_settings, get_paypal_settings
from lce.errors import ConfigError

router = APIRouter(prefix="/display", tags=["display"])

NOTE = (
    "The network is synthetic and generated in rupees. Amounts are shown at a declared "
    "display rate, not a market exchange rate."
)


class DisplayView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Literal["USD", "INR"]
    #: US dollars per analytic rupee, as a decimal string.
    usd_per_inr: str
    #: The PayPal sandbox currency and its scale per analytic rupee, if readable.
    sandbox_currency: str | None
    sandbox_scale: str | None
    #: Displayed dollars per sandbox dollar, when both are dollars: the sandbox
    #: pays 1/ratio of every displayed amount.
    sandbox_ratio: str | None
    note: str


def _plain(value: Decimal) -> str:
    """``Decimal('1E+3')`` -> ``'1000'``; ``Decimal('0.010')`` -> ``'0.01'``."""
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@router.get("", response_model=DisplayView, summary="How the interface prints money")
def display() -> DisplayView:
    d = get_display_settings()
    sandbox_currency: str | None = None
    sandbox_scale: Decimal | None = None
    try:
        p = get_paypal_settings()
        sandbox_currency, sandbox_scale = p.demo_currency, p.demo_scale
    except (ValidationError, ConfigError):
        # A broken PayPal section must not take the display contract down with it.
        pass
    ratio = (
        _plain(d.usd_per_inr / sandbox_scale)
        if sandbox_scale is not None and sandbox_currency == "USD"
        else None
    )
    return DisplayView(
        currency=d.currency,
        usd_per_inr=_plain(d.usd_per_inr),
        sandbox_currency=sandbox_currency,
        sandbox_scale=_plain(sandbox_scale) if sandbox_scale is not None else None,
        sandbox_ratio=ratio,
        note=NOTE,
    )
