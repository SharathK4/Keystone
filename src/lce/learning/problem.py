"""The Phase-3 learning problem: what is observed, and what is forbidden.

This module is the executable half of ``docs/PHASE3_DESIGN.md``. Everything a
Phase-3 model may consume passes through :class:`ObservedWindow`, and everything
it may not is either absent from that object or neutralised inside it.

The prediction origin
---------------------
The origin is the shock onset :math:`t_0 = \\min_c t_c`. That is the moment a
deployment learns something is wrong - "this merchant's inflow has failed" - and
has to answer *who else, and when*. Choosing the onset rather than ``t = 0`` is
what makes the problem well posed: before the onset there is nothing to predict,
and after it the answer is already unfolding in the event stream.

The observable filtration
-------------------------
.. math::

    \\mathcal{F}(t_0) = \\sigma\\big(
        \\{e : t_e < t_0\\},\\;
        \\{o : s_o < t_0\\},\\;
        \\{X_i\\},\\;
        \\tilde{S} \\big)

* payments **strictly before** the origin. Those in :math:`[0, t_0)` come from
  the *no-shock baseline run*: under common random numbers the shocked and
  unshocked worlds are identical before the onset, so this is the real
  pre-origin stream in both, not a counterfactual;
* the obligation book as issued, read off
  :attr:`~lce.benchmark.scenarios.BuiltScenario.unperturbed_graph` - never off
  ``scenario.graph``, whose deadlines and statuses the mutation families have
  already rewritten;
* static merchant disclosures :math:`X_i` - sector, tier, and (under the
  ``balance_sheet`` assumption) opening balance, credit line and operating floor;
* the shock descriptor :math:`\\tilde{S}` - which merchants, when, how much. This
  is the operator's trigger, not privileged knowledge.

What is deliberately withheld
-----------------------------
``pass_through``/``conditional_probability``/lag laws (the dependency overlay),
``payment_discipline``, ``exogenous_inflow_rate``, ``operating_burn_rate``,
``systemic_weight``, merchant ``metadata``, every event at or after the origin,
and the shock-perturbed obligation book.

The rates are the sharpest of these. A payments platform does not see a
merchant's payroll or rent, and :math:`b_i(0) / (\\mu_i - \\lambda_i)` - the
autonomy time - is very nearly the label. Handing those over would let a
three-line arithmetic model win, and would measure nothing about contagion.

Enforcement is structural, and then checked. The barrier is
:func:`build_observed_window` itself: it scrubs, filters and clears, so a feature
builder handed an :class:`ObservedWindow` physically cannot reach a forbidden
quantity. :func:`audit_window` then asserts directly that a given window contains
nothing it should not, and :func:`audit_leakage` guards against regressions in
the barrier by perturbing each forbidden input and requiring the feature matrix
to come back bit-identical.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from lce.benchmark.scenarios import BuiltScenario
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.domain.shock import Shock
from lce.errors import LeakageError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

#: Merchant attributes a deployment plausibly holds: identity, sector, size band
#: and - under the disclosure assumption - the balance sheet.
OBSERVABLE_PROFILE_FIELDS = frozenset(
    {
        "merchant_id",
        "external_id",
        "sector",
        "tier",
        "opening_balance",
        "credit_limit",
        "operating_floor",
    }
)

#: Latent parameters of the generator's cash and behaviour processes. Neutralised
#: to fixed constants in every observable view, so a builder that reaches for one
#: gets a constant rather than a signal.
LATENT_PROFILE_FIELDS = frozenset(
    {
        "payment_discipline",
        "exogenous_inflow_rate",
        "operating_burn_rate",
        "systemic_weight",
        "stress_threshold_ratio",
        "metadata",
        "name",
    }
)

#: Values the latent fields are replaced with. Chosen as the model defaults so a
#: scrubbed profile is still a valid :class:`MerchantProfile`.
_NEUTRAL: dict[str, Any] = {
    "payment_discipline": 0.9,
    "exogenous_inflow_rate": 0.0,
    "operating_burn_rate": 0.0,
    "systemic_weight": 1.0,
    "stress_threshold_ratio": 0.25,
    "metadata": {},
    "name": "",
}

#: Memoised observable graphs, keyed on ``(dataset, origin, observation spec)``.
WindowGraphCache = dict[
    tuple[str, float, str],
    tuple["TemporalPaymentGraph", dict[str, float], dict[str, float]],
]

#: Prediction horizons in hours, reported as a grid. The last entry is replaced
#: at build time by the remaining horizon, so every example also reports the
#: full-window number.
DEFAULT_HORIZON_GRID: tuple[float, ...] = (6.0, 24.0, 48.0, 72.0)


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    """Which observability assumptions are in force.

    Each flag names an assumption that can be switched off to produce an
    ablation, rather than a tuning knob. ``balance_sheet`` in particular is the
    one genuinely arguable disclosure in the design; the ablation measures what
    the results owe to it.
    """

    balance_sheet: bool = True
    shock_descriptor: bool = True
    graph_structure: bool = True
    lookback_hours: float | None = None
    """Truncate the observed history to this many hours before the origin.
    ``None`` uses everything available."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "balance_sheet": self.balance_sheet,
            "shock_descriptor": self.shock_descriptor,
            "graph_structure": self.graph_structure,
            "lookback_hours": self.lookback_hours,
        }


DEFAULT_OBSERVATION = ObservationSpec()


@dataclass(frozen=True, slots=True)
class PredictionTask:
    """The prediction targets and their discretisation.

    ``n_hazard_intervals`` partitions the remaining horizon for the discrete-time
    survival models. Eight intervals over a week is about 21 hours each - fine
    enough to separate a same-day cascade from a weekend one, coarse enough that
    each interval still carries events to fit on.
    """

    horizon_grid: tuple[float, ...] = DEFAULT_HORIZON_GRID
    n_hazard_intervals: int = 8

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_grid": list(self.horizon_grid),
            "n_hazard_intervals": self.n_hazard_intervals,
        }

    def grid_for(self, remaining_hours: float) -> tuple[float, ...]:
        """Horizon grid clipped to what is left, always including the full window."""
        inside = tuple(t for t in self.horizon_grid if t < remaining_hours)
        return (*inside, float(remaining_hours))

    def interval_edges(self, remaining_hours: float) -> np.ndarray:
        """Interval boundaries ``0 = e_0 < e_1 < ... < e_K = remaining``."""
        return np.linspace(0.0, float(remaining_hours), self.n_hazard_intervals + 1)


DEFAULT_TASK = PredictionTask()


def scrub_profile(
    profile: MerchantProfile, spec: ObservationSpec = DEFAULT_OBSERVATION
) -> MerchantProfile:
    """Return a copy with every latent attribute replaced by a constant.

    Under ``spec.balance_sheet = False`` the balance sheet is flattened too: all
    merchants get a unit buffer, so ratios to the buffer stop carrying any
    cross-sectional information. That is the ablation's whole point - it must not
    merely hide the number while leaving a rescaled version of it behind.
    """
    update = dict(_NEUTRAL)
    if not spec.balance_sheet:
        update |= {"opening_balance": 1.0, "credit_limit": 0.0, "operating_floor": 0.0}
    return profile.model_copy(update=update)


def is_observed(t: float, origin: float, cutoff: float) -> bool:
    """Whether an event at time ``t`` is inside the observable window.

    The temporal cutoff lives here and nowhere else, deliberately. It is the
    single line that separates the filtration from the future, so it is worth
    being a named function that a test can replace: the ``future_payments`` probe
    in :func:`audit_leakage` is only meaningful if there is something it can
    catch, and a negative control that widens this predicate is what demonstrates
    that it does.

    Strictly less than the origin. An event exactly *at* the onset is already the
    shock unfolding.
    """
    return cutoff <= t < origin


def _stable_event_id(event: PaymentEvent, index: int) -> str:
    """Content-addressed id for a simulator-emitted payment.

    The engine mints payment ids from ``uuid4``. That is harmless inside a run,
    but the graph orders events by ``(t, event_id)``, so folding raw simulator
    output into an observable view would make the feature matrix depend on
    process-local randomness whenever two payments share a timestamp. Re-keying
    on content restores determinism without touching the engine.
    """
    payload = (
        f"{event.payer_id}|{event.payee_id}|{event.amount:.6f}|{event.t:.6f}"
        f"|{event.obligation_id or ''}|{index}"
    )
    return "sim_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class ObservedWindow:
    """Everything a model may consume at one prediction origin.

    ``graph`` is an *observation*, not a simulable network: its profiles have had
    their cash-process parameters neutralised and its dependency overlay is
    empty. Simulating it would produce nonsense, which is the intended shape of
    the barrier - the object that crosses to the model side cannot be used to
    reconstruct the side it came from.
    """

    origin_t: float
    horizon_end: float
    graph: TemporalPaymentGraph
    shock: Shock | None
    paid_before_origin: dict[str, float]
    settled_at: dict[str, float]
    dataset_id: str
    scenario_id: str
    family: str
    spec: ObservationSpec = DEFAULT_OBSERVATION

    @property
    def remaining_hours(self) -> float:
        """``T - t_0``: how much of the horizon is still to be predicted."""
        return max(self.horizon_end - self.origin_t, 1e-6)

    @property
    def merchant_ids(self) -> list[str]:
        return sorted(self.graph.merchant_ids)

    def outstanding(self, obligation: Obligation) -> float:
        """Amount still owed at the origin, given what was paid before it."""
        return max(
            0.0, obligation.amount - self.paid_before_origin.get(obligation.obligation_id, 0.0)
        )

    def open_obligations(self) -> list[Obligation]:
        """Obligations with value still outstanding at the origin."""
        return [o for o in self.graph.obligations if self.outstanding(o) > 0.0]

    def state_graph(self, *, grace_hours: float = 48.0) -> TemporalPaymentGraph:
        """The observable graph with the obligation book rolled forward to the origin.

        :attr:`graph` carries the book *as issued* - every obligation still
        ``PENDING`` with nothing paid - because that is how the generator hands it
        over. Any mechanistic model reading it directly would treat invoices that
        were settled days ago as live exposure, and over-state everyone's
        payables. This applies the settlement facts the pre-origin payment stream
        already reveals, which leaks nothing: those payments are observed.
        """
        rolled = self.graph.copy()
        for obligation in self.graph.obligations:
            paid = self.paid_before_origin.get(obligation.obligation_id, 0.0)
            if paid <= 0.0:
                continue
            updated = obligation.model_copy(
                update={
                    "amount_paid": min(paid, obligation.amount),
                    "settled_t": self.settled_at.get(obligation.obligation_id),
                }
            )
            rolled.add_obligation(
                updated.touched(self.origin_t, grace_hours), require_nodes=False
            )
        return rolled

    def shock_by_node(self) -> dict[str, float]:
        """Directly-shocked magnitude per merchant, empty when the descriptor is off."""
        if self.shock is None or not self.spec.shock_descriptor:
            return {}
        totals: dict[str, float] = {}
        for component in self.shock.components:
            totals[component.merchant_id] = (
                totals.get(component.merchant_id, 0.0) + component.magnitude
            )
        return totals

    def summary(self) -> dict[str, Any]:
        stats = self.graph.stats()
        return {
            "dataset_id": self.dataset_id,
            "scenario_id": self.scenario_id,
            "family": self.family,
            "origin_t": self.origin_t,
            "remaining_hours": self.remaining_hours,
            "n_merchants": stats.n_merchants,
            "n_observed_events": stats.n_payment_events,
            "n_obligations": stats.n_obligations,
            "n_open_at_origin": len(self.open_obligations()),
            "observation": self.spec.to_dict(),
        }


def baseline_payment_stream(
    graph: TemporalPaymentGraph, config: SimulationConfig
) -> list[PaymentEvent]:
    """Payments the undisturbed network makes over the horizon.

    Run once per dataset and shared across its scenarios: the no-shock world does
    not depend on which shock is about to be applied, and re-running it per
    scenario would be the same computation many times over.
    """
    simulator = LiquiditySimulator(graph, config)
    simulator.run(None, run_id="observation-baseline")
    return simulator.emitted_payments


def build_observed_window(
    scenario: BuiltScenario,
    *,
    config: SimulationConfig,
    baseline_payments: Sequence[PaymentEvent] | None = None,
    origin_t: float | None = None,
    spec: ObservationSpec = DEFAULT_OBSERVATION,
    graph_cache: WindowGraphCache | None = None,
) -> ObservedWindow:
    """Assemble the observable view of one scenario at its prediction origin.

    ``baseline_payments`` are the no-shock stream for this dataset (see
    :func:`baseline_payment_stream`); when omitted it is computed here, which is
    correct but wasteful across a suite.

    ``graph_cache`` memoises the observable graph on
    ``(dataset, origin, observation spec)``. Several scenario families on one
    network resolve to the same origin, and the graph they see is then
    byte-identical - it depends on the pristine network and the cutoff, never on
    which shock is about to land. Sharing it saves both the rebuild and a
    duplicate copy of several thousand events per scenario. The shared graph is
    only ever read; the two callers that need to modify one
    (:meth:`ObservedWindow.state_graph` and the dependency estimator) copy first.
    """
    source = scenario.unperturbed_graph
    origin = float(scenario.shock.onset_t if origin_t is None else origin_t)
    horizon = float(config.horizon_hours)
    if origin >= horizon:
        raise LeakageError(
            f"prediction origin {origin:.1f}h is at or past the horizon {horizon:.1f}h; "
            "there is nothing left to predict",
            scenario_id=scenario.scenario_id,
        )

    key = (
        scenario.dataset_id,
        round(origin, 6),
        repr(sorted(spec.to_dict().items())),
    )
    if graph_cache is not None and key in graph_cache:
        cached_graph, cached_paid, cached_settled = graph_cache[key]
        return ObservedWindow(
            origin_t=origin,
            horizon_end=horizon,
            graph=cached_graph,
            shock=scenario.shock if spec.shock_descriptor else None,
            paid_before_origin=cached_paid,
            settled_at=cached_settled,
            dataset_id=scenario.dataset_id,
            scenario_id=scenario.scenario_id,
            family=str(scenario.spec.family),
            spec=spec,
        )

    stream = (
        list(baseline_payments)
        if baseline_payments is not None
        else baseline_payment_stream(source, config)
    )

    cutoff = -np.inf if spec.lookback_hours is None else origin - float(spec.lookback_hours)

    observed = TemporalPaymentGraph(
        network_id=f"{source.network_id}@obs{origin:.3f}",
        dataset_version=source.dataset_version,
        epoch_iso=source.epoch_iso,
    )
    observed.add_merchants(
        scrub_profile(profile, spec) for profile in source.merchants.values()
    )

    for event in source.payment_events:
        if is_observed(event.t, origin, cutoff):
            observed.add_payment(event, require_nodes=False)

    paid: dict[str, float] = {}
    settled_at: dict[str, float] = {}
    for index, event in enumerate(stream):
        if not is_observed(event.t, origin, -np.inf):
            continue
        if event.obligation_id:
            paid[event.obligation_id] = paid.get(event.obligation_id, 0.0) + event.amount
            settled_at[event.obligation_id] = max(
                settled_at.get(event.obligation_id, event.t), event.t
            )
        if is_observed(event.t, origin, cutoff):
            observed.add_payment(
                event.model_copy(update={"event_id": _stable_event_id(event, index)}),
                require_nodes=False,
            )

    for obligation in source.obligations:
        if obligation.issued_t < origin:
            observed.add_obligation(obligation, require_nodes=False)

    if graph_cache is not None:
        graph_cache[key] = (observed, paid, settled_at)

    return ObservedWindow(
        origin_t=origin,
        horizon_end=horizon,
        graph=observed,
        shock=scenario.shock if spec.shock_descriptor else None,
        paid_before_origin=paid,
        settled_at=settled_at,
        dataset_id=scenario.dataset_id,
        scenario_id=scenario.scenario_id,
        family=str(scenario.spec.family),
        spec=spec,
    )


# ------------------------------------------------------------------- the audit


FeatureBuilder = Callable[[ObservedWindow], np.ndarray]


@dataclass(slots=True)
class LeakageAudit:
    """Result of probing a feature builder for forbidden dependencies."""

    scenario_id: str
    probes: dict[str, bool] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return all(self.probes.values())

    def failures(self) -> list[str]:
        return sorted(name for name, ok in self.probes.items() if not ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "clean": self.clean,
            "probes": dict(self.probes),
            "failures": self.failures(),
        }


def _perturb_latent_profiles(
    graph: TemporalPaymentGraph, rng: np.random.Generator
) -> TemporalPaymentGraph:
    """Randomise every latent cash/behaviour parameter, leaving disclosures alone."""
    perturbed = graph.copy()
    for merchant_id, profile in graph.merchants.items():
        perturbed.add_merchant(
            profile.model_copy(
                update={
                    "payment_discipline": float(rng.uniform(0.05, 1.0)),
                    "exogenous_inflow_rate": float(rng.uniform(0.0, 5e4)),
                    "operating_burn_rate": float(rng.uniform(0.0, 5e4)),
                    "systemic_weight": float(rng.uniform(0.1, 20.0)),
                    "stress_threshold_ratio": float(rng.uniform(0.0, 2.0)),
                    "metadata": {"leak_probe": merchant_id},
                    "name": f"probe-{merchant_id}",
                }
            )
        )
    return perturbed


def _perturb_future_payments(
    stream: Sequence[PaymentEvent], origin: float, rng: np.random.Generator
) -> list[PaymentEvent]:
    """Scramble amounts and times at or after the origin; leave the past intact."""
    out: list[PaymentEvent] = []
    for event in stream:
        if event.t < origin:
            out.append(event)
            continue
        out.append(
            event.model_copy(
                update={
                    "amount": float(max(1.0, event.amount * rng.uniform(0.1, 10.0))),
                    "t": float(origin + rng.uniform(0.0, 1e3)),
                }
            )
        )
    return out


def audit_leakage(
    build_features: FeatureBuilder,
    scenario: BuiltScenario,
    *,
    config: SimulationConfig,
    baseline_payments: Sequence[PaymentEvent],
    spec: ObservationSpec = DEFAULT_OBSERVATION,
    seed: int = 20250101,
    raise_on_failure: bool = True,
) -> LeakageAudit:
    """Probe the whole pipeline for dependence on anything it must not see.

    What this does and does not prove
    ---------------------------------
    The barrier is :func:`build_observed_window`: it scrubs the latent profile
    fields, drops every event at or after the origin, empties the dependency
    overlay, and reads the obligation book off the *unperturbed* graph. Given a
    correctly built window, a feature builder physically cannot reach a
    forbidden quantity, so these probes are not a search for a rogue builder -
    they are **regression guards on the barrier itself**. Weaken the scrub,
    slip the cutoff from ``<`` to ``<=``, or read ``scenario.graph`` instead of
    ``scenario.unperturbed_graph``, and the corresponding probe fails.

    Three counterfactuals, each of which the output must be a constant function
    of:

    ``latent_profiles``
        randomise ``payment_discipline``, the exogenous inflow and burn rates,
        the systemic weight and the metadata on the source network.

    ``future_payments``
        scramble every payment at or after the origin.

    ``true_dependencies``
        install a random dependency overlay on the source graph.

    The comparison is exact rather than tolerant: any movement at all is a leak.
    :func:`audit_window` makes the complementary, direct assertions about what a
    window contains.
    """
    rng = np.random.default_rng(seed)
    reference = build_features(
        build_observed_window(
            scenario,
            config=config,
            baseline_payments=baseline_payments,
            spec=spec,
        )
    )

    def _matches(variant: BuiltScenario, stream: Sequence[PaymentEvent]) -> bool:
        candidate = build_features(
            build_observed_window(
                variant, config=config, baseline_payments=stream, spec=spec
            )
        )
        return candidate.shape == reference.shape and bool(
            np.array_equal(
                np.nan_to_num(candidate, nan=-1.0), np.nan_to_num(reference, nan=-1.0)
            )
        )

    origin = float(scenario.shock.onset_t)
    source = scenario.unperturbed_graph

    probes = {
        "latent_profiles": _matches(
            replace(scenario, baseline_graph=_perturb_latent_profiles(source, rng)),
            baseline_payments,
        ),
        "future_payments": _matches(
            scenario, _perturb_future_payments(baseline_payments, origin, rng)
        ),
        "true_dependencies": _matches(
            replace(scenario, baseline_graph=_with_random_dependencies(source, rng)),
            baseline_payments,
        ),
    }

    audit = LeakageAudit(scenario_id=scenario.scenario_id, probes=probes)
    if raise_on_failure and not audit.clean:
        raise LeakageError(
            f"feature builder leaks: {', '.join(audit.failures())}",
            scenario_id=scenario.scenario_id,
        )
    return audit


def audit_window(
    window: ObservedWindow, scenario: BuiltScenario, *, raise_on_failure: bool = True
) -> LeakageAudit:
    """Assert directly that a window contains nothing forbidden.

    The complement to :func:`audit_leakage`. That function asks whether the
    output *moves* when a forbidden input changes; this one opens the window and
    checks what is actually inside it:

    ``no_future_events``    every payment is strictly before the origin.
    ``no_dependency_edges`` the overlay is empty - nothing to read off.
    ``profiles_scrubbed``   every latent field holds its neutral constant.
    ``unperturbed_book``    the obligation book matches the pristine graph, not
                            the mutated one. This is the probe that catches the
                            subtle case: ``DELAYED_INFLOW`` and
                            ``SUPPLIER_FAILURE`` rewrite deadlines and statuses
                            in ``scenario.graph`` from ``t = 0``, so a window
                            built from it would show the model the shock before
                            it happened.
    """
    graph = window.graph
    checks: dict[str, bool] = {
        "no_future_events": all(e.t < window.origin_t for e in graph.payment_events),
        "no_dependency_edges": not graph.dependency_edges,
        "profiles_scrubbed": all(
            all(
                getattr(profile, field) == neutral
                for field, neutral in _NEUTRAL.items()
            )
            for profile in graph.merchants.values()
        ),
    }

    pristine = {
        o.obligation_id: (round(o.due_t, 6), str(o.status), round(o.amount, 6))
        for o in scenario.unperturbed_graph.obligations
    }
    observed = {
        o.obligation_id: (round(o.due_t, 6), str(o.status), round(o.amount, 6))
        for o in graph.obligations
    }
    checks["unperturbed_book"] = all(
        pristine.get(key) == value for key, value in observed.items()
    )

    audit = LeakageAudit(scenario_id=window.scenario_id, probes=checks)
    if raise_on_failure and not audit.clean:
        raise LeakageError(
            f"observable window is contaminated: {', '.join(audit.failures())}",
            scenario_id=window.scenario_id,
        )
    return audit


def _with_random_dependencies(
    graph: TemporalPaymentGraph, rng: np.random.Generator
) -> TemporalPaymentGraph:
    """Install a random dependency overlay - the probe for reading the answer."""
    from lce.domain.edges import DependencyEdge, LagDistribution

    perturbed = graph.copy()
    perturbed.clear_dependencies()
    pairs = [
        (payer, payee)
        for payer, payee in graph.distinct_pairs()
        if EXTERNAL_SINK not in (payer, payee)
        and graph.has_merchant(payer)
        and graph.has_merchant(payee)
    ]
    for source, target in pairs:
        perturbed.set_dependency(
            DependencyEdge(
                source_id=source,
                target_id=target,
                pass_through=float(rng.uniform(0.0, 1.0)),
                conditional_probability=float(rng.uniform(0.0, 1.0)),
                reliability=float(rng.uniform(0.0, 1.0)),
                lag=LagDistribution.from_mean_cv(float(rng.uniform(1.0, 400.0)), cv=1.0),
                is_ground_truth=True,
            )
        )
    return perturbed
