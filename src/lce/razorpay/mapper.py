"""Mapping Razorpay entities onto the domain model.

Unit conversion
---------------
Razorpay reports amounts in **paise** (integer minor units). The domain math
core works in rupees as floats. All conversion happens here, so a factor-of-100
error can only ever be introduced in one place.

Identity resolution
-------------------
A Razorpay payment names a payer and payee by Razorpay identifiers (contact ids,
account ids, order notes), not by our merchant ids. The mapping is supplied by
the caller as a resolver, because it depends on how a given deployment onboarded
its merchants - and guessing it silently would attach real money to the wrong
node. Unresolvable counterparties map to the external sink, which keeps the cash
accounting correct while ensuring no fictitious contagion edge is created.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from lce.domain.base import to_sim_time
from lce.domain.enums import PaymentChannel, PaymentStatus
from lce.domain.events import EXTERNAL_SINK, PaymentEvent
from lce.errors import ValidationError

PAISE_PER_RUPEE = 100.0

# Razorpay `method` -> our channel vocabulary.
_METHOD_TO_CHANNEL: dict[str, PaymentChannel] = {
    "upi": PaymentChannel.UPI,
    "card": PaymentChannel.CARD,
    "netbanking": PaymentChannel.NETBANKING,
    "wallet": PaymentChannel.WALLET,
    "emi": PaymentChannel.CARD,
    "bank_transfer": PaymentChannel.NEFT,
    "neft": PaymentChannel.NEFT,
    "rtgs": PaymentChannel.RTGS,
    "imps": PaymentChannel.IMPS,
}

_STATUS_MAP: dict[str, PaymentStatus] = {
    "captured": PaymentStatus.CAPTURED,
    "authorized": PaymentStatus.AUTHORIZED,
    "failed": PaymentStatus.FAILED,
    "refunded": PaymentStatus.REFUNDED,
    "processed": PaymentStatus.SETTLED,
    "reversed": PaymentStatus.FAILED,
}

MerchantResolver = Callable[[str | None], str | None]


def paise_to_rupees(amount: int | float | None) -> float:
    if amount is None:
        return 0.0
    return float(amount) / PAISE_PER_RUPEE


def rupees_to_paise(amount: float) -> int:
    return round(amount * PAISE_PER_RUPEE)


def map_channel(method: str | None) -> PaymentChannel:
    return _METHOD_TO_CHANNEL.get((method or "").lower(), PaymentChannel.OTHER)


def map_status(status: str | None) -> PaymentStatus:
    return _STATUS_MAP.get((status or "").lower(), PaymentStatus.CAPTURED)


def payment_from_entity(
    entity: dict[str, Any],
    epoch: datetime,
    *,
    resolve_merchant: MerchantResolver,
    default_payee: str | None = None,
    settlement_lag_hours: float = 0.0,
) -> PaymentEvent:
    """Convert one Razorpay payment/payout/transfer entity into a PaymentEvent.

    ``resolve_merchant`` maps a Razorpay identifier to one of our merchant ids,
    returning ``None`` when the counterparty is outside the modelled network.
    """
    entity_id = entity.get("id")
    if not entity_id:
        raise ValidationError("Razorpay entity has no id")

    amount = paise_to_rupees(entity.get("amount"))
    if amount <= 0:
        raise ValidationError(
            f"Razorpay entity {entity_id} has a non-positive amount", amount=amount
        )

    created = entity.get("created_at")
    occurred = (
        datetime.fromtimestamp(int(created), tz=UTC)
        if isinstance(created, int | float)
        else datetime.now(tz=UTC)
    )

    notes = entity.get("notes") or {}
    payer_hint = (
        notes.get("payer_merchant_id")
        or entity.get("customer_id")
        or entity.get("contact_id")
        or entity.get("source")
    )
    payee_hint = (
        notes.get("payee_merchant_id")
        or entity.get("account_id")
        or entity.get("recipient")
        or default_payee
    )

    payer = resolve_merchant(payer_hint) or EXTERNAL_SINK
    payee = resolve_merchant(payee_hint) or default_payee or EXTERNAL_SINK

    if payer == payee:
        raise ValidationError(
            f"Razorpay entity {entity_id} resolves payer and payee to the same "
            f"merchant {payer!r}; check the resolver mapping",
            entity_id=entity_id,
        )

    return PaymentEvent(
        payer_id=payer,
        payee_id=payee,
        amount=amount,
        t=to_sim_time(occurred, epoch),
        channel=map_channel(entity.get("method")),
        status=map_status(entity.get("status")),
        settlement_lag_hours=settlement_lag_hours,
        external_id=str(entity_id),
        is_synthetic=False,
        metadata={
            "source": "razorpay",
            "order_id": entity.get("order_id"),
            "currency": entity.get("currency", "INR"),
            "method": entity.get("method"),
            "unresolved_payer": payer == EXTERNAL_SINK,
            "unresolved_payee": payee == EXTERNAL_SINK,
        },
    )


def identity_resolver(mapping: dict[str, str]) -> MerchantResolver:
    """Resolver backed by an explicit ``{razorpay_id: merchant_id}`` dict."""

    def _resolve(key: str | None) -> str | None:
        return mapping.get(key) if key else None

    return _resolve
