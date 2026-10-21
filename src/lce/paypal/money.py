"""Money for execution, and the one rule that converts analysis into it.

Two kinds of amount exist in this system and they never share a number:

* **Analytic** amounts - rupees, produced by the model and replayed in the
  synthetic network. Floats, as everywhere in the analysis.
* **Executed** amounts - what is actually sent to PayPal's sandbox. ``Decimal``
  from the moment they are created, quantised to the currency's minor unit, and
  strings on the wire.

PayPal cannot pay out INR, so an analytic amount crosses into execution through
:class:`DemoScale`: a declared factor in units of the sandbox currency per rupee,
applied once, rounded once, by one stated rule (:data:`ROUNDING`). It is a
display scale, not an exchange rate, and is labelled as such wherever it shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

#: Banker's rounding to the currency's minor unit. Stated once and used for
#: every conversion, so "executed = analytic x scale" is checkable to within
#: half a minor unit.
ROUNDING = ROUND_HALF_EVEN

#: Currencies PayPal treats as having no minor unit. Everything else uses two
#: decimal places for the purposes of this module.
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})


def minor_exponent(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL_CURRENCIES else 2


def _quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-minor_exponent(currency))


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in one currency.

    Construction refuses ``float`` outright: a float that reaches this type has
    already lost its exactness, and accepting it would hide where.
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"Money needs a Decimal, got {type(self.amount).__name__}")
        if not self.amount.is_finite():
            raise ValueError("Money must be finite")
        code = self.currency
        if len(code) != 3 or not code.isalpha() or not code.isupper():
            raise ValueError(f"not an ISO-4217 code: {code!r}")
        if self.amount != self.amount.quantize(_quantum(code)):
            raise ValueError(f"{self.amount} has more precision than {code} allows")

    @classmethod
    def of(cls, value: str | int | Decimal, currency: str) -> Money:
        try:
            amount = Decimal(value) if not isinstance(value, Decimal) else value
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal amount: {value!r}") from exc
        return cls(amount.quantize(_quantum(currency), rounding=ROUNDING), currency)

    @property
    def minor_units(self) -> int:
        return int(self.amount.scaleb(minor_exponent(self.currency)))

    @classmethod
    def from_minor(cls, minor: int, currency: str) -> Money:
        return cls(Decimal(minor).scaleb(-minor_exponent(currency)), currency)

    def to_wire(self) -> dict[str, str]:
        """PayPal's ``currency`` object: an ISO code and a string value."""
        return {"currency": self.currency, "value": f"{self.amount:f}"}

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> Money:
        return cls.of(str(wire["value"]), str(wire["currency"]))

    def __add__(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise ValueError(f"cannot add {other.currency} to {self.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __str__(self) -> str:
        return f"{self.currency} {self.amount:f}"


@dataclass(frozen=True, slots=True)
class DemoScale:
    """Analytic rupees -> sandbox currency, by one declared factor."""

    rate: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Decimal) or self.rate <= 0:
            raise ValueError("demo scale must be a positive Decimal")
        if self.currency == "INR":
            raise ValueError("PayPal cannot pay out INR")

    def apply(self, analytic_inr: float | Decimal) -> Money:
        """Scale one analytic amount. The float is read through ``repr``.

        ``Decimal(str(x))`` reads the shortest decimal that round-trips to the
        float, which is what was printed, logged and shown everywhere else; the
        binary expansion ``Decimal(x)`` would be exact about the wrong thing.
        """
        exact = analytic_inr if isinstance(analytic_inr, Decimal) else Decimal(repr(analytic_inr))
        return Money.of(exact * self.rate, self.currency)

    def describe(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "scale": f"{self.rate:f}",
            "unit": f"{self.currency} per analytic INR",
            "rounding": "half-even to the currency's minor unit",
        }
