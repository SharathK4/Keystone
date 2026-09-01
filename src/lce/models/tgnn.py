"""Temporal graph neural network for contagion prediction.

What it learns
--------------
Given a network and a shock, predict per merchant (a) whether the shock will
leave it unable to meet an obligation within the horizon, and (b) when. The
analytic models in :mod:`lce.models.propagation` encode a *fixed* propagation
rule; this one learns the rule from simulated cascades, so it can pick up
effects the analytic rule has no way to express - obligation timing interacting
with buffer depth, several weak paths compounding, the difference between a node
that is merely downstream and one that is downstream *and* thinly capitalised.

Architecture
------------
``GATv2Conv`` message passing with edge features. Attention is the right
inductive bias here: a node with six suppliers is not equally exposed through
all six, and the attention weights are exactly the "which upstream link matters"
quantity the demo needs to surface. Edge attributes (pass-through, reliability,
lag, live obligation value) are fed into the attention computation via
``edge_dim`` rather than being collapsed into a scalar weight.

Messages flow along the **reverse** of the payment direction. Payments go
``i -> j``; contagion exposure at ``j`` is caused by ``i``, so for the network to
aggregate "what is happening upstream of me", ``j`` must receive messages from
``i``. Getting this backwards silently trains a model that propagates influence
the wrong way down the supply chain.

Two heads share the trunk: a binary exposure logit and a hit-time regression.
The hit-time loss is masked to genuinely-affected nodes - regressing a time for
nodes that were never hit would be fitting a label that does not exist.

torch and torch_geometric are imported lazily so the rest of the system runs
without the ML extra installed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from lce.domain.enums import PredictorKind
from lce.domain.events import EXTERNAL_SINK
from lce.domain.prediction import ModelPrediction, NodeExposure
from lce.domain.propagation import CascadeResult
from lce.domain.shock import Shock
from lce.errors import DependencyUnavailableError, ModelError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.seeds import config_hash, seed_everything

if TYPE_CHECKING:  # pragma: no cover
    import torch

logger = get_logger(__name__)

NODE_FEATURE_DIM = 16
EDGE_FEATURE_DIM = 8
MODEL_FORMAT_VERSION = 1


def _require_torch() -> tuple[Any, Any]:
    """Import torch / PyG on demand, with an actionable error if absent."""
    try:
        import torch
        from torch_geometric.nn import GATv2Conv
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise DependencyUnavailableError(
            "The temporal GNN needs the 'ml' extra. Install it with: "
            "pip install -e '.[ml]'",
            missing=str(exc),
        ) from exc
    return torch, GATv2Conv


@dataclass(frozen=True, slots=True)
class TGNNConfig:
    """Architecture and optimisation hyper-parameters."""

    hidden_dim: int = 64
    num_layers: int = 3
    heads: int = 4
    dropout: float = 0.1
    learning_rate: float = 3e-3
    weight_decay: float = 1e-4
    epochs: int = 120
    patience: int = 25
    hit_time_weight: float = 0.25
    positive_class_weight: float = 6.0
    horizon_hours: float = 168.0
    threshold: float = 0.5
    seed: int = 20250101

    # Input widths. Defaulted to the built-in builders' output so existing
    # callers are unaffected; Phase 3 supplies its own leak-free feature tables,
    # which are wider, and needs the trunk sized to them.
    node_feature_dim: int = NODE_FEATURE_DIM
    edge_feature_dim: int = EDGE_FEATURE_DIM

    def to_dict(self) -> dict[str, Any]:
        return {
            "hidden_dim": self.hidden_dim,
            "num_layers": self.num_layers,
            "heads": self.heads,
            "dropout": self.dropout,
            "learning_rate": self.learning_rate,
            "weight_decay": self.weight_decay,
            "epochs": self.epochs,
            "patience": self.patience,
            "hit_time_weight": self.hit_time_weight,
            "positive_class_weight": self.positive_class_weight,
            "horizon_hours": self.horizon_hours,
            "threshold": self.threshold,
            "seed": self.seed,
            "node_feature_dim": self.node_feature_dim,
            "edge_feature_dim": self.edge_feature_dim,
        }


# --------------------------------------------------------------------- features


def _safe_log(x: float) -> float:
    return math.log1p(max(0.0, x))


def build_node_features(
    graph: TemporalPaymentGraph, shock: Shock, horizon: float
) -> tuple[list[str], np.ndarray]:
    """Per-merchant feature matrix, conditioned on the shock.

    Monetary quantities enter as ``log1p`` and as *ratios to the node's own
    buffer*. The ratios are what generalise: a network of anchors and micro
    merchants spans four orders of magnitude in rupees, and raw amounts would
    let the model learn "big node" instead of "exposed node".
    """
    ids = sorted(graph.merchant_ids)
    index = {m: i for i, m in enumerate(ids)}
    features = np.zeros((len(ids), NODE_FEATURE_DIM), dtype=np.float32)

    payables: dict[str, float] = dict.fromkeys(ids, 0.0)
    receivables: dict[str, float] = dict.fromkeys(ids, 0.0)
    for obligation in graph.obligations:
        if obligation.due_t > horizon or not obligation.is_open:
            continue
        if obligation.debtor_id in payables:
            payables[obligation.debtor_id] += obligation.outstanding
        if obligation.creditor_id in receivables:
            receivables[obligation.creditor_id] += obligation.outstanding

    shock_by_node: dict[str, float] = {}
    for component in shock.components:
        shock_by_node[component.merchant_id] = (
            shock_by_node.get(component.merchant_id, 0.0) + component.magnitude
        )

    for merchant_id in ids:
        profile = graph.merchant(merchant_id)
        row = index[merchant_id]
        buffer = max(profile.initial_buffer, 1.0)
        payable = payables[merchant_id]
        receivable = receivables[merchant_id]
        shock_mag = shock_by_node.get(merchant_id, 0.0)

        out_edges = graph.out_dependencies(merchant_id)
        in_edges = graph.in_dependencies(merchant_id)
        autonomy = profile.autonomy_hours()

        features[row] = np.array(
            [
                _safe_log(buffer),
                _safe_log(profile.opening_balance),
                _safe_log(payable),
                _safe_log(receivable),
                payable / buffer,
                receivable / buffer,
                shock_mag / buffer,
                1.0 if shock_mag > 0 else 0.0,
                len(in_edges) / 10.0,
                len(out_edges) / 10.0,
                profile.payment_discipline,
                _safe_log(profile.systemic_weight),
                profile.exogenous_inflow_rate * horizon / buffer,
                profile.operating_burn_rate * horizon / buffer,
                min(autonomy, 5000.0) / 5000.0 if math.isfinite(autonomy) else 1.0,
                float(sum(e.pass_through for e in out_edges)),
            ],
            dtype=np.float32,
        )

    return ids, np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)


def build_edge_features(
    graph: TemporalPaymentGraph, ids: Sequence[str], horizon: float
) -> tuple[np.ndarray, np.ndarray]:
    """Edge index (reversed for contagion) and edge attributes.

    Returns ``(edge_index, edge_attr)`` where ``edge_index[0]`` is the message
    *source*. Payments run ``i -> j``; exposure at ``j`` is driven by ``i``, so
    messages must travel ``i -> j`` in the contagion sense, which means the
    payer is the message source. The reversal is therefore implicit: we emit
    ``(payer, payee)`` as ``(src, dst)`` for message passing, which is the
    opposite of what a cash-flow adjacency would use.
    """
    index = {m: i for i, m in enumerate(ids)}

    live: dict[tuple[str, str], float] = {}
    for obligation in graph.obligations:
        if obligation.due_t > horizon or not obligation.is_open:
            continue
        key = (obligation.debtor_id, obligation.creditor_id)
        live[key] = live.get(key, 0.0) + obligation.outstanding

    src: list[int] = []
    dst: list[int] = []
    attrs: list[list[float]] = []
    for edge in graph.dependency_edges:
        if edge.source_id not in index or edge.target_id not in index:
            continue
        if EXTERNAL_SINK in edge.key:
            continue
        src.append(index[edge.source_id])
        dst.append(index[edge.target_id])
        obligation_value = live.get(edge.key, 0.0)
        attrs.append(
            [
                edge.pass_through,
                edge.reliability,
                edge.conditional_probability,
                _safe_log(edge.lag.mean_hours) / 10.0,
                min(edge.lag.sigma_log, 5.0) / 5.0,
                _safe_log(edge.features.mean_amount) / 25.0,
                edge.features.regularity,
                _safe_log(obligation_value) / 25.0,
            ]
        )

    if not src:
        return (
            np.zeros((2, 0), dtype=np.int64),
            np.zeros((0, EDGE_FEATURE_DIM), dtype=np.float32),
        )

    edge_index = np.array([src, dst], dtype=np.int64)
    edge_attr = np.nan_to_num(
        np.array(attrs, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0
    )
    return edge_index, edge_attr


def build_labels(
    ids: Sequence[str],
    cascade: CascadeResult,
    baseline: CascadeResult | None,
    horizon: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Targets: ``(affected, hit_time, hit_mask)``.

    Labels are *shock-attributable*: nodes that also fail in the undisturbed
    baseline are excluded, so the model is trained on contagion rather than on
    the network's background failure rate.
    """
    baseline_affected = set(baseline.affected_ids) if baseline is not None else set()
    affected_set = set(cascade.affected_ids) - baseline_affected
    hit_times = cascade.hit_times()

    y = np.zeros(len(ids), dtype=np.float32)
    t = np.zeros(len(ids), dtype=np.float32)
    mask = np.zeros(len(ids), dtype=np.float32)
    for i, merchant_id in enumerate(ids):
        if merchant_id in affected_set:
            y[i] = 1.0
            hit = hit_times.get(merchant_id)
            if hit is not None:
                t[i] = min(hit, horizon) / horizon
                mask[i] = 1.0
    return y, t, mask


# ----------------------------------------------------------------------- model


def _build_module(config: TGNNConfig) -> Any:
    """Construct the network. Defined as a factory so torch imports stay lazy."""
    torch, GATv2Conv = _require_torch()
    nn = torch.nn

    class ContagionGNN(nn.Module):
        """Shared GATv2 trunk with exposure and hit-time heads."""

        def __init__(self, cfg: TGNNConfig) -> None:
            super().__init__()
            self.cfg = cfg
            self.input_norm = nn.LayerNorm(cfg.node_feature_dim)
            self.encoder = nn.Linear(cfg.node_feature_dim, cfg.hidden_dim)

            self.convs = nn.ModuleList()
            self.norms = nn.ModuleList()
            for _ in range(cfg.num_layers):
                self.convs.append(
                    GATv2Conv(
                        cfg.hidden_dim,
                        cfg.hidden_dim // cfg.heads,
                        heads=cfg.heads,
                        edge_dim=cfg.edge_feature_dim,
                        dropout=cfg.dropout,
                        add_self_loops=True,
                    )
                )
                self.norms.append(nn.LayerNorm(cfg.hidden_dim))

            self.dropout = nn.Dropout(cfg.dropout)
            self.exposure_head = nn.Sequential(
                nn.Linear(cfg.hidden_dim, cfg.hidden_dim // 2),
                nn.ReLU(),
                nn.Linear(cfg.hidden_dim // 2, 1),
            )
            self.hit_time_head = nn.Sequential(
                nn.Linear(cfg.hidden_dim, cfg.hidden_dim // 2),
                nn.ReLU(),
                nn.Linear(cfg.hidden_dim // 2, 1),
                nn.Sigmoid(),  # normalised hit time in [0, 1] x horizon
            )

        def forward(
            self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor
        ) -> tuple[torch.Tensor, torch.Tensor]:
            h = self.encoder(self.input_norm(x))
            for conv, norm in zip(self.convs, self.norms, strict=True):
                # Residual connection: contagion depth varies per node, and the
                # skip lets shallow nodes keep their own features instead of
                # being over-smoothed by many rounds of aggregation.
                h = norm(h + self.dropout(torch.relu(conv(h, edge_index, edge_attr))))
            return self.exposure_head(h).squeeze(-1), self.hit_time_head(h).squeeze(-1)

    return ContagionGNN(config)


@dataclass(slots=True)
class TrainingSample:
    """One (network state, shock) pair with its simulated labels."""

    node_ids: list[str]
    x: np.ndarray
    edge_index: np.ndarray
    edge_attr: np.ndarray
    y: np.ndarray
    hit_time: np.ndarray
    hit_mask: np.ndarray
    shock_id: str | None = None


def make_sample(
    graph: TemporalPaymentGraph,
    shock: Shock,
    cascade: CascadeResult,
    baseline: CascadeResult | None,
    horizon: float,
) -> TrainingSample:
    ids, x = build_node_features(graph, shock, horizon)
    edge_index, edge_attr = build_edge_features(graph, ids, horizon)
    y, hit_time, hit_mask = build_labels(ids, cascade, baseline, horizon)
    return TrainingSample(
        node_ids=ids,
        x=x,
        edge_index=edge_index,
        edge_attr=edge_attr,
        y=y,
        hit_time=hit_time,
        hit_mask=hit_mask,
        shock_id=shock.shock_id,
    )


class TemporalGNNPredictor:
    """Trainable contagion predictor over the temporal dependency graph."""

    kind = PredictorKind.TEMPORAL_GNN

    def __init__(self, config: TGNNConfig | None = None) -> None:
        self.config = config or TGNNConfig()
        self._model: Any = None
        self._trained = False
        self.history: list[dict[str, float]] = []

    @property
    def model_version(self) -> str:
        return f"tgnn-{config_hash(self.config.to_dict(), length=10)}"

    @property
    def is_trained(self) -> bool:
        return self._trained

    # ---------------------------------------------------------------- training

    def fit(
        self,
        train: Sequence[TrainingSample],
        validation: Sequence[TrainingSample] = (),
    ) -> dict[str, Any]:
        """Train on simulated cascades, early-stopping on validation loss."""
        if not train:
            raise ModelError("cannot train the temporal GNN on an empty sample set")

        torch, _ = _require_torch()
        seed_everything(self.config.seed)
        cfg = self.config

        self._model = _build_module(cfg)
        optimiser = torch.optim.AdamW(
            self._model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
        )
        # Affected nodes are a small minority of the network, so an unweighted
        # BCE would be minimised by predicting "nobody is affected".
        pos_weight = torch.tensor([cfg.positive_class_weight], dtype=torch.float32)
        bce = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)

        train_tensors = [self._to_tensors(s) for s in train]
        val_tensors = [self._to_tensors(s) for s in validation]

        best_loss = float("inf")
        best_state: dict[str, Any] | None = None
        stale = 0
        self.history = []

        for epoch in range(cfg.epochs):
            self._model.train()
            total = 0.0
            for batch in train_tensors:
                optimiser.zero_grad()
                loss = self._loss(batch, bce, torch)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self._model.parameters(), 5.0)
                optimiser.step()
                total += float(loss.item())
            train_loss = total / len(train_tensors)

            val_loss = train_loss
            if val_tensors:
                self._model.eval()
                with torch.no_grad():
                    val_loss = sum(
                        float(self._loss(b, bce, torch).item()) for b in val_tensors
                    ) / len(val_tensors)

            self.history.append(
                {"epoch": float(epoch), "train_loss": train_loss, "val_loss": val_loss}
            )

            if val_loss < best_loss - 1e-5:
                best_loss = val_loss
                best_state = {
                    k: v.detach().clone() for k, v in self._model.state_dict().items()
                }
                stale = 0
            else:
                stale += 1
                if stale >= cfg.patience:
                    break

        if best_state is not None:
            self._model.load_state_dict(best_state)
        self._trained = True

        summary = {
            "epochs_run": len(self.history),
            "best_val_loss": best_loss,
            "final_train_loss": self.history[-1]["train_loss"] if self.history else None,
            "n_train": len(train),
            "n_validation": len(validation),
            "model_version": self.model_version,
        }
        logger.info("tgnn_trained", **summary)
        return summary

    def _to_tensors(self, sample: TrainingSample) -> dict[str, Any]:
        torch, _ = _require_torch()
        return {
            "x": torch.from_numpy(sample.x),
            "edge_index": torch.from_numpy(sample.edge_index),
            "edge_attr": torch.from_numpy(sample.edge_attr),
            "y": torch.from_numpy(sample.y),
            "hit_time": torch.from_numpy(sample.hit_time),
            "hit_mask": torch.from_numpy(sample.hit_mask),
        }

    def _loss(self, batch: dict[str, Any], bce: Any, torch: Any) -> Any:
        logits, hit = self._model(batch["x"], batch["edge_index"], batch["edge_attr"])
        loss = bce(logits, batch["y"])
        mask = batch["hit_mask"]
        if float(mask.sum()) > 0:
            # Masked so hit-time error is only charged where a hit happened.
            timing = torch.sum(((hit - batch["hit_time"]) ** 2) * mask) / mask.sum()
            loss = loss + self.config.hit_time_weight * timing
        return loss

    # -------------------------------------------------------------- inference

    def predict_from_sample(
        self,
        sample: TrainingSample,
        *,
        run_id: str | None = None,
        shock_id: str | None = None,
        horizon_hours: float | None = None,
    ) -> ModelPrediction:
        """Predict from a pre-built sample instead of re-deriving features.

        :meth:`predict` builds its inputs with this module's own feature
        functions, which read the dependency overlay and the merchant profile
        directly. Phase 3 cannot use that path - its features have to come from
        the observable window - so it assembles a :class:`TrainingSample` itself
        and enters here. The trunk, the weights and the output contract are the
        same either way.
        """
        if not self._trained or self._model is None:
            raise ModelError("temporal GNN must be trained (or loaded) before predicting")

        torch, _ = _require_torch()
        horizon = horizon_hours if horizon_hours is not None else self.config.horizon_hours

        self._model.eval()
        with torch.no_grad():
            logits, hit = self._model(
                torch.from_numpy(sample.x),
                torch.from_numpy(sample.edge_index),
                torch.from_numpy(sample.edge_attr),
            )
            scores = torch.sigmoid(logits).numpy()
            hit_times = hit.numpy() * horizon

        exposures = {
            merchant_id: NodeExposure(
                merchant_id=merchant_id,
                exposure_score=float(np.clip(scores[i], 0.0, 1.0)),
                expected_hit_t=float(hit_times[i]),
            )
            for i, merchant_id in enumerate(sample.node_ids)
        }
        return ModelPrediction(
            run_id=run_id,
            shock_id=shock_id or sample.shock_id,
            predictor=self.kind,
            model_version=self.model_version,
            horizon_hours=horizon,
            threshold=self.config.threshold,
            exposures=exposures,
            seed=self.config.seed,
            config_hash=config_hash(self.config.to_dict()),
            metadata={"source": "sample"},
        )

    def predict(
        self,
        graph: TemporalPaymentGraph,
        shock: Shock,
        *,
        run_id: str | None = None,
    ) -> ModelPrediction:
        if not self._trained or self._model is None:
            raise ModelError("temporal GNN must be trained (or loaded) before predicting")

        torch, _ = _require_torch()
        cfg = self.config
        horizon = cfg.horizon_hours

        ids, x = build_node_features(graph, shock, horizon)
        edge_index, edge_attr = build_edge_features(graph, ids, horizon)

        self._model.eval()
        with torch.no_grad():
            logits, hit = self._model(
                torch.from_numpy(x),
                torch.from_numpy(edge_index),
                torch.from_numpy(edge_attr),
            )
            scores = torch.sigmoid(logits).numpy()
            hit_times = hit.numpy() * horizon

        exposures = {
            merchant_id: NodeExposure(
                merchant_id=merchant_id,
                exposure_score=float(np.clip(scores[i], 0.0, 1.0)),
                expected_hit_t=float(hit_times[i]) if scores[i] >= cfg.threshold else None,
            )
            for i, merchant_id in enumerate(ids)
        }

        return ModelPrediction(
            run_id=run_id,
            shock_id=shock.shock_id,
            predictor=self.kind,
            model_version=self.model_version,
            horizon_hours=horizon,
            threshold=cfg.threshold,
            exposures=exposures,
            seed=cfg.seed,
            config_hash=config_hash(cfg.to_dict()),
            metadata={"tgnn_config": cfg.to_dict()},
        )

    # ------------------------------------------------------------ persistence

    def save(self, path: Path) -> Path:
        if not self._trained or self._model is None:
            raise ModelError("refusing to save an untrained temporal GNN")
        torch, _ = _require_torch()
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "format_version": MODEL_FORMAT_VERSION,
                "config": self.config.to_dict(),
                "state_dict": self._model.state_dict(),
                "history": self.history,
                "node_feature_dim": NODE_FEATURE_DIM,
                "edge_feature_dim": EDGE_FEATURE_DIM,
            },
            path,
        )
        return path

    @classmethod
    def load(cls, path: Path) -> TemporalGNNPredictor:
        torch, _ = _require_torch()
        payload = torch.load(path, map_location="cpu", weights_only=False)
        if payload.get("format_version") != MODEL_FORMAT_VERSION:
            raise ModelError(
                f"unsupported model format {payload.get('format_version')!r}; "
                f"expected {MODEL_FORMAT_VERSION}"
            )
        predictor = cls(TGNNConfig(**payload["config"]))
        predictor._model = _build_module(predictor.config)
        predictor._model.load_state_dict(payload["state_dict"])
        predictor._model.eval()
        predictor._trained = True
        predictor.history = payload.get("history", [])
        return predictor
