"""The display contract: currency, declared rate and the sandbox ratio.

Presentation only, but the interface prints every amount through it, so the
default, the override and the ratio to the sandbox scale are pinned here.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from lce.config import (
    DisplaySettings,
    reset_display_settings_cache,
    reset_paypal_settings_cache,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ("LCE_DISPLAY_CURRENCY", "LCE_DISPLAY_USD_PER_INR",
                "PAYPAL_DEMO_CURRENCY", "PAYPAL_DEMO_SCALE"):
        monkeypatch.delenv(key, raising=False)
    reset_display_settings_cache()
    reset_paypal_settings_cache()
    yield
    reset_display_settings_cache()
    reset_paypal_settings_cache()


def test_defaults_are_dollars_at_a_round_declared_rate() -> None:
    s = DisplaySettings(_env_file=None)
    assert s.currency == "USD"
    assert s.usd_per_inr == Decimal("0.01")


def test_rate_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LCE_DISPLAY_USD_PER_INR", "0")
    with pytest.raises(ValueError):
        DisplaySettings(_env_file=None)


def test_route_serves_rate_and_sandbox_ratio(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LCE_DISPLAY_CURRENCY", "USD")
    monkeypatch.setenv("LCE_DISPLAY_USD_PER_INR", "0.01")
    monkeypatch.setenv("PAYPAL_DEMO_CURRENCY", "USD")
    monkeypatch.setenv("PAYPAL_DEMO_SCALE", "0.00001")
    body = api_client.get("/api/v1/display").json()
    assert body["currency"] == "USD"
    assert body["usd_per_inr"] == "0.01"
    assert body["sandbox_currency"] == "USD"
    assert body["sandbox_scale"] == "0.00001"
    # Every displayed dollar is paid as a thousandth of a dollar in the sandbox.
    assert body["sandbox_ratio"] == "1000"
    assert "not a market exchange rate" in body["note"]


def test_rupees_on_request_and_no_ratio_across_currencies(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LCE_DISPLAY_CURRENCY", "INR")
    monkeypatch.setenv("PAYPAL_DEMO_CURRENCY", "EUR")
    body = api_client.get("/api/v1/display").json()
    assert body["currency"] == "INR"
    assert body["sandbox_currency"] == "EUR"
    assert body["sandbox_ratio"] is None


def test_broken_paypal_section_does_not_break_display(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # INR is refused as a sandbox currency; the display contract still answers.
    monkeypatch.setenv("PAYPAL_DEMO_CURRENCY", "INR")
    response = api_client.get("/api/v1/display")
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox_currency"] is None
    assert body["sandbox_ratio"] is None
