"""Provider-agnostic execution of a chosen intervention.

The optimiser decides what to do; a provider decides where that happens. Keeping
them apart is what lets every experiment in this repository run with no payment
provider configured, while leaving a real integration point for one.
"""

from __future__ import annotations

from lce.execution.providers import (
    PROVIDERS,
    ExecutionError,
    ExecutionProvider,
    ExecutionRecord,
    RazorpayTestProvider,
    SimulationProvider,
    build_provider,
    execute_plan,
)

__all__ = [
    "PROVIDERS",
    "ExecutionError",
    "ExecutionProvider",
    "ExecutionRecord",
    "RazorpayTestProvider",
    "SimulationProvider",
    "build_provider",
    "execute_plan",
]
