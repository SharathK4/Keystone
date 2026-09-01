"""Latent payment-dependency inference.

The problem
-----------
We observe only a stream of payments. We want the *conditional* structure
behind it: when merchant ``i`` receives cash, how much of it flows onward to
``j``, how likely is that to happen at all, and how long does it take?

Those three quantities - :math:`\\theta_{ij}` (pass-through), :math:`q_{ij}`
(conditional probability) and the lag law - are latent, and they are confounded
by a *baseline* stream: ``i`` also pays ``j`` on a recurring schedule regardless
of any inflow. An estimator that divides total outflow by total inflow
attributes those baseline payments to excitation and over-states dependence.

The estimator
-------------
Each edge is fitted as a **marked Hawkes process** whose exogenous events are the
inflows to ``i`` - see :mod:`lce.models.hawkes` for the model and the EM
derivation. From the converged responsibilities we read off:

``pass_through``             :math:`\\hat\\theta_{ij}` - responsibility-weighted
                             geometric mean of :math:`a_o / a_m` over
                             parent-child payment pairs.
``conditional_probability``  :math:`\\hat q_{ij} = \\min(1, \\alpha_{ij})` - expected
                             number of payments triggered per inflow.
``lag law``                  responsibility-weighted log-normal fit to the
                             parent-to-child gaps.
``base_intensity``           :math:`\\hat\\mu_{ij}` - the recurring stream that has
                             nothing to do with any inflow.

Splitting :math:`\\theta` from :math:`q` is the point. Reporting only the product
(forwarded value over total inflow value) cannot distinguish "forwards rarely
but a lot" from "forwards often but a little", and those two propagate shocks
completely differently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from lce.domain.edges import DependencyEdge, LagDistribution
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.models.features import compute_edge_features, estimate_reliability
from lce.models.hawkes import fit_marked_hawkes

logger = get_logger(__name__)

ESTIMATOR_NAME = "marked_hawkes_em"


@dataclass(frozen=True, slots=True)
class DependencyLearnerConfig:
    """Estimator hyper-parameters, recorded in every run manifest."""

    em_iterations: int = 40
    em_tolerance: float = 1e-6
    max_parents_per_event: int = 64
    kernel_window_multiple: float = 8.0
    max_lag_hours: float = 24.0 * 30
    min_events_for_fit: int = 3

    theta_init: float = 0.4
    sigma_init: float = 0.8
    alpha_init: float = 0.5
    alpha_max: float = 5.0
    prior_lag_hours: float = 48.0
    refit_beta: bool = True
    prune_below_pass_through: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimator": ESTIMATOR_NAME,
            "em_iterations": self.em_iterations,
            "em_tolerance": self.em_tolerance,
            "max_parents_per_event": self.max_parents_per_event,
            "kernel_window_multiple": self.kernel_window_multiple,
            "max_lag_hours": self.max_lag_hours,
            "min_events_for_fit": self.min_events_for_fit,
            "theta_init": self.theta_init,
            "sigma_init": self.sigma_init,
            "alpha_init": self.alpha_init,
            "alpha_max": self.alpha_max,
            "prior_lag_hours": self.prior_lag_hours,
            "refit_beta": self.refit_beta,
            "prune_below_pass_through": self.prune_below_pass_through,
        }


class DependencyLearner:
    """Fits the conditional dependency structure from an event stream."""

    def __init__(self, config: DependencyLearnerConfig | None = None) -> None:
        self.config = config or DependencyLearnerConfig()

    # ------------------------------------------------------------------ public

    def fit_graph(
        self,
        graph: TemporalPaymentGraph,
        *,
        t_end: float = 0.0,
        settled_obligations: Sequence[Obligation] = (),
    ) -> list[DependencyEdge]:
        """Estimate a dependency edge for every ordered pair that transacted.

        ``t_end`` bounds the observation window; events at or after it are held
        out. The default of 0.0 uses only the historical window, keeping the
        simulation horizon strictly unseen by the learner.
        """
        history = [e for e in graph.payment_events if e.t < t_end]
        if not history:
            return []

        t_start = min(e.t for e in history)
        window = max(t_end - t_start, 1.0)

        # The inflow index deliberately includes payments arriving from outside
        # the modelled merchant set (consumer revenue). Those are real cash
        # arrivals and are exactly what excites a node's outgoing payments;
        # excluding them would leave anchor merchants with no observable driver
        # and make their pass-through unidentifiable. Dependency *edges* are
        # still fitted only between real merchants, since a shock cannot
        # propagate into the external sink.
        inflow_times: dict[str, list[float]] = {}
        inflow_amounts: dict[str, list[float]] = {}
        by_pair: dict[tuple[str, str], list[PaymentEvent]] = {}
        for event in history:
            inflow_times.setdefault(event.payee_id, []).append(event.t)
            inflow_amounts.setdefault(event.payee_id, []).append(event.amount)
            if EXTERNAL_SINK in (event.payer_id, event.payee_id):
                continue
            by_pair.setdefault((event.payer_id, event.payee_id), []).append(event)

        settled_by_pair: dict[tuple[str, str], list[Obligation]] = {}
        for obligation in settled_obligations:
            settled_by_pair.setdefault(
                (obligation.debtor_id, obligation.creditor_id), []
            ).append(obligation)

        cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        edges: list[DependencyEdge] = []
        for (source, target), events in sorted(by_pair.items()):
            if source not in cache:
                raw_t = np.asarray(inflow_times.get(source, []), dtype=float)
                raw_a = np.asarray(inflow_amounts.get(source, []), dtype=float)
                order = np.argsort(raw_t, kind="stable")
                cache[source] = (raw_t[order], raw_a[order])

            edge = self._fit_edge(
                source,
                target,
                events,
                *cache[source],
                window=window,
                settled=settled_by_pair.get((source, target), []),
            )
            if edge.pass_through >= self.config.prune_below_pass_through:
                edges.append(edge)

        logger.info(
            "dependency_fit_complete",
            estimator=ESTIMATOR_NAME,
            n_edges=len(edges),
            n_events=len(history),
            window_hours=window,
        )
        return edges

    # ----------------------------------------------------------------- private

    def _fit_edge(
        self,
        source: str,
        target: str,
        events: Sequence[PaymentEvent],
        inflow_times: np.ndarray,
        inflow_amounts: np.ndarray,
        *,
        window: float,
        settled: Sequence[Obligation],
    ) -> DependencyEdge:
        cfg = self.config
        features = compute_edge_features(events)
        ordered = sorted(events, key=lambda e: e.t)
        outflow_times = np.array([e.t for e in ordered], dtype=float)
        outflow_amounts = np.array([e.amount for e in ordered], dtype=float)

        reliability, basis = estimate_reliability(settled, features=features)

        if len(ordered) < cfg.min_events_for_fit or inflow_times.size == 0:
            # Not enough evidence to separate excitation from baseline. Report a
            # pure-baseline edge with zero confidence rather than a noisy
            # dependence estimate that downstream code would treat as real.
            return DependencyEdge(
                source_id=source,
                target_id=target,
                features=features,
                lag=LagDistribution.from_mean_cv(cfg.prior_lag_hours, cv=1.0),
                reliability=reliability,
                pass_through=0.0,
                conditional_probability=0.0,
                excitation_alpha=0.0,
                base_intensity=len(ordered) / max(window, 1.0),
                is_ground_truth=False,
                estimator=ESTIMATOR_NAME,
                confidence=0.0,
                metadata={
                    "n_outflows": len(ordered),
                    "n_inflows": int(inflow_times.size),
                    "underdetermined": True,
                    "reliability_basis": basis,
                },
            )

        fit = fit_marked_hawkes(
            outflow_times,
            outflow_amounts,
            inflow_times,
            inflow_amounts,
            window=window,
            beta=1.0 / max(cfg.prior_lag_hours, 1e-3),
            theta_init=cfg.theta_init,
            sigma_init=cfg.sigma_init,
            alpha_init=cfg.alpha_init,
            alpha_max=cfg.alpha_max,
            max_parents=cfg.max_parents_per_event,
            kernel_window_multiple=cfg.kernel_window_multiple,
            iterations=cfg.em_iterations,
            tolerance=cfg.em_tolerance,
            refit_beta=cfg.refit_beta,
        )

        lag = (
            LagDistribution(
                mu_log=fit.lag_mu_log,
                sigma_log=max(fit.lag_sigma_log, 1e-2),
                floor_hours=0.0,
                max_hours=cfg.max_lag_hours,
            )
            if fit.lag_weight > 1.0
            else LagDistribution.from_mean_cv(cfg.prior_lag_hours, cv=1.0)
        )

        evidence = min(1.0, fit.excitation_mass / 12.0)
        confidence = float(evidence * (1.0 if fit.converged else 0.6))

        return DependencyEdge(
            source_id=source,
            target_id=target,
            features=features,
            lag=lag,
            reliability=reliability,
            pass_through=float(np.clip(fit.theta, 0.0, 1.0)),
            conditional_probability=float(np.clip(fit.conditional_probability, 0.0, 1.0)),
            excitation_alpha=float(max(0.0, fit.alpha)),
            excitation_decay=float(max(fit.beta, 1e-6)),
            base_intensity=float(max(0.0, fit.mu)),
            is_ground_truth=False,
            estimator=ESTIMATOR_NAME,
            confidence=confidence,
            metadata={
                "n_outflows": int(outflow_times.size),
                "n_inflows": int(inflow_times.size),
                "excitation_mass": fit.excitation_mass,
                "background_mass": fit.background_mass,
                "sigma_ratio": fit.sigma_ratio,
                "log_likelihood": fit.log_likelihood,
                "em_iterations": fit.iterations,
                "converged": fit.converged,
                "reliability_basis": basis,
            },
        )


def learn_dependencies(
    graph: TemporalPaymentGraph,
    config: DependencyLearnerConfig | None = None,
    *,
    t_end: float = 0.0,
    install: bool = False,
) -> list[DependencyEdge]:
    """Fit dependency edges; optionally install them onto the graph overlay."""
    edges = DependencyLearner(config).fit_graph(graph, t_end=t_end)
    if install:
        graph.clear_dependencies()
        graph.set_dependencies(edges)
    return edges


def compare_to_ground_truth(
    estimated: Sequence[DependencyEdge],
    truth: dict[tuple[str, str], DependencyEdge],
) -> dict[str, float]:
    """Score the learner against the generator's true parameters.

    Reports edge recovery (did we find the right links?) and parameter error on
    links present in both. Rank correlation matters most for contagion: the
    propagation model consumes the *relative* strength of edges, so getting the
    ordering right dominates getting the absolute level right.
    """
    est_map = {e.key: e for e in estimated}
    shared = sorted(set(est_map) & set(truth))

    result: dict[str, float] = {
        "n_estimated": float(len(est_map)),
        "n_true": float(len(truth)),
        "n_matched": float(len(shared)),
        "edge_precision": len(shared) / len(est_map) if est_map else 0.0,
        "edge_recall": len(shared) / len(truth) if truth else 0.0,
    }
    if not shared:
        return result

    est_theta = np.array([est_map[k].pass_through for k in shared])
    true_theta = np.array([truth[k].pass_through for k in shared])
    est_q = np.array([est_map[k].conditional_probability for k in shared])
    true_q = np.array([truth[k].conditional_probability for k in shared])
    est_lag = np.array([est_map[k].lag.mean_hours for k in shared])
    true_lag = np.array([truth[k].lag.mean_hours for k in shared])

    result |= {
        "pass_through_mae": float(np.mean(np.abs(est_theta - true_theta))),
        "pass_through_rmse": float(np.sqrt(np.mean((est_theta - true_theta) ** 2))),
        "pass_through_bias": float(np.mean(est_theta - true_theta)),
        "pass_through_corr": _safe_corr(est_theta, true_theta),
        "pass_through_spearman": _spearman(est_theta, true_theta),
        "conditional_prob_mae": float(np.mean(np.abs(est_q - true_q))),
        "conditional_prob_corr": _safe_corr(est_q, true_q),
        "lag_mae_hours": float(np.mean(np.abs(est_lag - true_lag))),
        "lag_corr": _safe_corr(est_lag, true_lag),
        "lag_spearman": _spearman(est_lag, true_lag),
    }
    return result


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation that returns 0.0 instead of NaN on constant input."""
    if a.size < 2 or float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation - the ranking is what the propagation model consumes."""
    if a.size < 2:
        return 0.0
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return _safe_corr(ra, rb)
