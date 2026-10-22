"""The execution record's state machine.

Webhooks arrive late, twice, and out of order; a reconcile ``GET`` can race a
webhook. Every one of them goes through :func:`advance`, so the record's status
depends only on *what* was learned, never on the order it arrived in - except
where two contradicting terminal facts arrive, in which case the first one
recorded stands and the second is logged, not applied.

Ranks::

    unknown (0) < submitted (1) < pending (2) < held (3) < succeeded | failed (4)

* A status only moves **up**. A ``pending`` after ``held`` is an older fact
  arriving late and is ignored.
* ``succeeded`` and ``failed`` are **terminal**: nothing changes them. A later
  event is recorded in the ledger with the reason it was not applied.
* ``planned`` was never sent, so no provider fact can apply to it.

Applying the same fact twice is a no-op, which is what makes duplicate delivery
harmless.
"""

from __future__ import annotations

from dataclasses import dataclass

RANK = {"unknown": 0, "submitted": 1, "pending": 2, "held": 3, "succeeded": 4, "failed": 4}
TERMINAL = frozenset({"succeeded", "failed"})
STATUSES = frozenset(RANK) | {"planned"}


@dataclass(frozen=True, slots=True)
class Transition:
    status: str
    changed: bool
    reason: str


def advance(current: str, incoming: str) -> Transition:
    """The record status after learning ``incoming``, and why."""
    if incoming not in RANK:
        return Transition(current, False, f"not a provider status: {incoming!r}")
    if current == "planned":
        return Transition(current, False, "recorded as a plan; nothing was sent to PayPal")
    if current in TERMINAL:
        if incoming == current:
            return Transition(current, False, "already recorded")
        return Transition(current, False, f"already {current}; a later {incoming} is not applied")
    if incoming == current:
        return Transition(current, False, "already recorded")
    if RANK[incoming] < RANK[current]:
        return Transition(current, False, f"{incoming} is older than {current}; not applied")
    return Transition(incoming, True, f"{current} -> {incoming}")
