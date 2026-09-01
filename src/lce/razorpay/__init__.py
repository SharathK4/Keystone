"""Razorpay integration: read-only client, webhook verification, payload mapping.

Scope note: this package intentionally has **no** money-moving surface. The
system models liquidity and recommends interventions; executing one is a human
action taken in the Razorpay dashboard. Keeping disbursal out of the code makes
that boundary structural.
"""

from __future__ import annotations

from lce.razorpay.client import RazorpayClient
from lce.razorpay.mapper import (
    identity_resolver,
    map_channel,
    map_status,
    paise_to_rupees,
    payment_from_entity,
    rupees_to_paise,
)
from lce.razorpay.webhooks import (
    EVENT_ID_HEADER,
    PAYMENT_EVENTS,
    SIGNATURE_HEADER,
    WebhookEnvelope,
    compute_signature,
    parse_webhook,
    require_signature,
    verify_signature,
)

__all__ = [
    "EVENT_ID_HEADER",
    "PAYMENT_EVENTS",
    "SIGNATURE_HEADER",
    "RazorpayClient",
    "WebhookEnvelope",
    "compute_signature",
    "identity_resolver",
    "map_channel",
    "map_status",
    "paise_to_rupees",
    "parse_webhook",
    "payment_from_entity",
    "require_signature",
    "rupees_to_paise",
    "verify_signature",
]
