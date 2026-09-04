"""Serving-side forward pass: state in, calibrated contagion out.

Two responsibilities, and nothing else. Turn a request payload into the same
observable view the model was fitted on, and run the fitted weights over it.

Reconstructing the observable view
----------------------------------
A request carries merchants, the obligation book, the pre-cutoff payment stream
and the shock. That is exactly the content of a Phase-3
:class:`~lce.learning.problem.ObservedWindow`, so one is assembled directly
rather than a second feature path being written. Everything after that -
:func:`~lce.learning.features.build_node_features`,
:func:`~lce.learning.features.build_interval_features`,
:func:`~lce.learning.features.build_hazard_design` - is the *same code* the
training pipeline ran. A separate serving implementation of those would be a
second definition of the model's inputs, and the two would diverge.

Events at or after the cutoff are dropped here as well. A caller that posts its
whole history, cutoff included, gets a prediction from before the cutoff -
serving must not be more permissive than training, or the deployed model is
quietly answering a different question than the one it was scored on.

The forward pass
----------------
.. math::

    h_{ik} = \\sigma(z_{ik}^\\top w), \\qquad
    F_i(t) = 1 - \\prod_{k:\\, e_k \\le t} (1 - h_{ik})

then the calibration map fitted on validation, then the expected time-to-
constraint as the mean of the implied conditional density. Deterministic: no
sampling, no dropout, no thread-dependent reductions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lce.domain.enums import MerchantSector, MerchantTier
from lce.domain.events import Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.errors import ModelError, ValidationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.inference.artifact import ModelArtifact
from lce.learning.features import (
    FEATURE_SCHEMA_VERSION,
    ObservedStats,
    build_hazard_design,
    build_interval_features,
    build_node_features,
)
from lce.learning.problem import ObservationSpec, ObservedWindow, PredictionTask, is_observed


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))


def apply_calibrator(
    probabilities: np.ndarray, artifact: ModelArtifact
) -> np.ndarray:
    """Apply the calibration map recorded in the bundle.

    Reimplemented from the stored parameters rather than by importing the
    training-side calibrator classes: the map is two scalars or two arrays, and
    the point of the artifact is that serving needs nothing from the training
    package to use it.
    """
    kind = artifact.manifest.calibrator.get("calibrator", "identity")
    if kind == "identity":
        return probabilities
    if kind == "platt":
        slope = float(artifact.manifest.calibrator.get("slope", 1.0))
        intercept = float(artifact.manifest.calibrator.get("intercept", 0.0))
        clipped = np.clip(probabilities, 1e-6, 1.0 - 1e-6)
        return _sigmoid(slope * np.log(clipped / (1.0 - clipped)) + intercept)
    if kind == "isotonic":
        if artifact.calibrator_x is None or artifact.calibrator_y is None:
            raise ModelError("isotonic calibration selected but no knots were exported")
        return np.interp(
            probabilities,
            artifact.calibrator_x,
            artifact.calibrator_y,
            left=float(artifact.calibrator_y[0]),
            right=float(artifact.calibrator_y[-1]),
        )
    raise ModelError(f"unknown calibrator {kind!r} in artifact manifest")


# --------------------------------------------------------------- state -> window


@dataclass(slots=True)
class NetworkState:
    """The observable state a caller posts. Plain data, no simulator objects."""

    merchants: list[MerchantProfile] = field(default_factory=list)
    obligations: list[Obligation] = field(default_factory=list)
    payments: list[PaymentEvent] = field(default_factory=list)
    network_id: str = "request"

    def to_graph(self) -> TemporalPaymentGraph:
        graph = TemporalPaymentGraph(network_id=self.network_id)
        graph.add_merchants(self.merchants)
        graph.add_payments(self.payments, require_nodes=False)
        graph.add_obligations(self.obligations, require_nodes=False)
        return graph


def build_request_window(
    state: NetworkState,
    *,
    shock: Any,
    observation_cutoff: float,
    horizon_hours: float,
    spec: ObservationSpec = ObservationSpec(),
) -> ObservedWindow:
    """Assemble the observable window a request describes.

    The cutoff is enforced here, not trusted from the caller: payments at or
    after it are dropped and obligations issued after it are excluded, so a
    request cannot obtain a prediction that used information the model was never
    allowed to see at fit time.
    """
    if horizon_hours <= observation_cutoff:
        raise ValidationError(
            f"horizon {horizon_hours} must be after the observation cutoff "
            f"{observation_cutoff}; there is nothing left to predict",
        )

    graph = TemporalPaymentGraph(network_id=state.network_id)
    graph.add_merchants(state.merchants)

    paid: dict[str, float] = {}
    settled_at: dict[str, float] = {}
    for event in state.payments:
        if not is_observed(event.t, observation_cutoff, -math.inf):
            continue
        graph.add_payment(event, require_nodes=False)
        if event.obligation_id:
            paid[event.obligation_id] = paid.get(event.obligation_id, 0.0) + event.amount
            settled_at[event.obligation_id] = max(
                settled_at.get(event.obligation_id, event.t), event.t
            )

    for obligation in state.obligations:
        if obligation.issued_t < observation_cutoff:
            graph.add_obligation(obligation, require_nodes=False)

    return ObservedWindow(
        origin_t=float(observation_cutoff),
        horizon_end=float(horizon_hours),
        graph=graph,
        shock=shock,
        paid_before_origin=paid,
        settled_at=settled_at,
        dataset_id=state.network_id,
        scenario_id=getattr(shock, "shock_id", "request"),
        family="request",
        spec=spec,
    )


# ------------------------------------------------------------------ predictions


@dataclass(slots=True)
class NodePrediction:
    """One merchant's answer."""

    merchant_id: str
    probability_constrained: float
    expected_time_to_constraint_hours: float | None
    probability_by: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "probability_constrained": self.probability_constrained,
            "expected_time_to_constraint_hours": self.expected_time_to_constraint_hours,
            "probability_by": dict(self.probability_by),
        }


@dataclass(slots=True)
class ContagionPrediction:
    """The full answer, plus what produced it."""

    nodes: list[NodePrediction] = field(default_factory=list)
    interval_edges: list[float] = field(default_factory=list)
    model_version: str = ""
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    artifact_hash: str = ""
    calibrator: str = "identity"
    threshold: float = 0.5
    observation_cutoff: float = 0.0
    horizon_hours: float = 0.0

    def scores(self) -> dict[str, float]:
        return {n.merchant_id: n.probability_constrained for n in self.nodes}

    def hit_times(self) -> dict[str, float]:
        return {
            n.merchant_id: n.expected_time_to_constraint_hours
            for n in self.nodes
            if n.expected_time_to_constraint_hours is not None
        }

    def flagged(self) -> list[str]:
        return sorted(
            n.merchant_id for n in self.nodes if n.probability_constrained >= self.threshold
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "artifact_hash": self.artifact_hash,
            "calibrator": self.calibrator,
            "threshold": self.threshold,
            "observation_cutoff": self.observation_cutoff,
            "horizon_hours": self.horizon_hours,
            "interval_edges": self.interval_edges,
            "n_flagged": len(self.flagged()),
            "nodes": [n.to_dict() for n in self.nodes],
        }


class HazardPredictor:
    """Runs an exported discrete-time hazard bundle. Stateless per request."""

    def __init__(self, artifact: ModelArtifact) -> None:
        if artifact.manifest.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise ModelError(
                f"artifact schema {artifact.manifest.feature_schema_version!r} does "
                f"not match this build's {FEATURE_SCHEMA_VERSION!r}",
            )
        self.artifact = artifact
        self.task = PredictionTask(n_hazard_intervals=artifact.manifest.n_hazard_intervals)

    @property
    def model_version(self) -> str:
        return self.artifact.model_version

    def predict(
        self, window: ObservedWindow, *, horizon_grid: Sequence[float] | None = None
    ) -> ContagionPrediction:
        """Per-node probability of constraint and expected time to it."""
        stats = ObservedStats.build(window)
        ids, x = build_node_features(window, stats)
        edges = self.task.interval_edges(window.remaining_hours)
        interval_x = build_interval_features(window, edges, stats)

        design = build_hazard_design(
            x,
            interval_x,
            remaining_hours=window.remaining_hours,
            feature_mask=self.artifact.feature_mask,
        )
        # Width is checked before anything is multiplied. A mismatch here means
        # the bundle was fitted on a different feature set, and the failure has
        # to say that rather than surfacing as a numpy broadcast error three
        # lines later.
        if design.shape[-1] != self.artifact.weights.shape[-1]:
            raise ModelError(
                f"design width {design.shape[-1]} does not match the artifact's "
                f"{self.artifact.weights.shape[-1]}; the bundle was fitted on a "
                "different feature set",
                runtime_width=int(design.shape[-1]),
                artifact_width=int(self.artifact.weights.shape[-1]),
            )

        # The intercept column is appended after standardisation, exactly as at
        # fit time - standardising a constant column would zero it.
        body = design[:, :, :-1]
        standardised = (body - self.artifact.mean) / self.artifact.scale
        full = np.concatenate([standardised, np.ones((*standardised.shape[:-1], 1))], axis=-1)

        hazards = _sigmoid(full @ self.artifact.weights)
        survival = np.cumprod(1.0 - hazards, axis=1)
        cdf = np.clip(np.maximum.accumulate(1.0 - survival, axis=1), 0.0, 1.0)
        calibrated = np.clip(apply_calibrator(cdf[:, -1], self.artifact), 0.0, 1.0)

        # Conditional expected time: the mean of the implied density, which is
        # only meaningful given that the event happens at all.
        starts, ends = edges[:-1], edges[1:]
        mids = 0.5 * (starts + ends)
        density = np.diff(cdf, axis=1, prepend=0.0)
        mass = density.sum(axis=1)
        expected = np.where(mass > 1e-12, density @ mids, float(mids[-1]))

        grid = horizon_grid if horizon_grid is not None else self.task.grid_for(
            window.remaining_hours
        )
        knots = np.concatenate(([0.0], ends))

        nodes: list[NodePrediction] = []
        for row, merchant_id in enumerate(ids):
            curve = np.concatenate(([0.0], cdf[row]))
            # Rescale every horizon slice by the same calibration factor the
            # full-window probability received, so the curve stays monotone and
            # consistent with the number the model is scored on.
            factor = calibrated[row] / max(cdf[row, -1], 1e-12)
            nodes.append(
                NodePrediction(
                    merchant_id=merchant_id,
                    probability_constrained=float(calibrated[row]),
                    expected_time_to_constraint_hours=float(expected[row]),
                    probability_by={
                        f"{t:.0f}h": float(np.clip(np.interp(t, knots, curve) * factor, 0.0, 1.0))
                        for t in grid
                    },
                )
            )

        return ContagionPrediction(
            nodes=nodes,
            interval_edges=[float(e) for e in edges],
            model_version=self.model_version,
            feature_schema_version=self.artifact.manifest.feature_schema_version,
            artifact_hash=self.artifact.manifest.content_hash,
            calibrator=str(self.artifact.manifest.calibrator.get("calibrator", "identity")),
            threshold=float(self.artifact.manifest.threshold),
            observation_cutoff=window.origin_t,
            horizon_hours=window.horizon_end,
        )


# --------------------------------------------------------------- input coercion


def merchant_from_payload(payload: dict[str, Any]) -> MerchantProfile:
    """Build a merchant profile from request data, defaulting the latent fields.

    The latent behaviour parameters are *not* accepted from a request even if a
    caller supplies them: the model was fitted on scrubbed profiles, so serving
    must scrub too, or the deployed feature vector differs from the trained one.
    """
    return MerchantProfile(
        merchant_id=payload["merchant_id"],
        sector=MerchantSector(payload.get("sector", MerchantSector.OTHER)),
        tier=MerchantTier(payload.get("tier", MerchantTier.SMALL)),
        opening_balance=float(payload["opening_balance"]),
        credit_limit=float(payload.get("credit_limit", 0.0)),
        operating_floor=float(payload.get("operating_floor", 0.0)),
    )
