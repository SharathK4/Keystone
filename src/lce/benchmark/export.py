"""Benchmark dataset export and reload.

A dataset directory is self-describing:

    <root>/<dataset_id>/
        manifest.json            regeneration record (params, seed, versions)
        merchants.parquet        node table
        payments.parquet         the event fact table
        obligations.parquet      commitments
        dependency_edges.parquet ground-truth overlay  [withheld from models]
        scenarios/<scenario_id>/
            scenario.json        spec, targets, mutations
            ground_truth.json    latent truth  [withheld from models]

Ground truth is written to **separate files** rather than mixed into the tables
a model consumes. That is the whole point: a loader can hand a model the
observable tables and physically not have the answers in scope.

Parquet is the default because the payment table is the large one and columnar
storage with dictionary encoding handles it far better than CSV; CSV is offered
for inspection and for tools that cannot read Parquet.

Streaming
---------
:func:`stream_payments_to_parquet` writes the event table in row-group batches
without ever materialising the full frame. At LARGE scale the payment table
dominates memory, so this is the difference between a dataset that exports and
one that runs the machine out of RAM.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from lce.benchmark.manifest import DatasetManifest
from lce.data.generator import SyntheticNetwork
from lce.domain.edges import DependencyEdge, LagDistribution
from lce.domain.enums import (
    MerchantSector,
    MerchantTier,
    ObligationKind,
    ObligationStatus,
    PaymentChannel,
    PaymentStatus,
)
from lce.domain.events import Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.errors import NotFoundError, ValidationError
from lce.graph.builders import build_graph
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger

logger = get_logger(__name__)

ExportFormat = Literal["parquet", "csv"]

MERCHANTS_FILE = "merchants"
PAYMENTS_FILE = "payments"
OBLIGATIONS_FILE = "obligations"
EDGES_FILE = "dependency_edges"
SCENARIOS_DIR = "scenarios"

# Rows per Parquet row group when streaming. Large enough to keep the file
# efficient, small enough that peak memory stays bounded.
STREAM_BATCH_ROWS = 50_000

# Canonical column order of the payment fact table. Declared once so the
# streaming writer and the empty-table fallback cannot drift apart.
PAYMENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "payer_id",
    "payee_id",
    "amount",
    "t",
    "obligation_id",
    "channel",
    "status",
    "settlement_lag_hours",
    "external_id",
    "is_synthetic",
    "driver",
)


@dataclass(slots=True)
class ExportResult:
    """What an export wrote."""

    directory: Path
    fmt: str
    files: dict[str, str]
    rows: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "format": self.fmt,
            "files": self.files,
            "rows": self.rows,
        }


# ------------------------------------------------------------------- frames


def merchants_frame(graph: TemporalPaymentGraph) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "merchant_id": p.merchant_id,
                "external_id": p.external_id,
                "name": p.name,
                "sector": str(p.sector),
                "tier": str(p.tier),
                "opening_balance": p.opening_balance,
                "operating_floor": p.operating_floor,
                "credit_limit": p.credit_limit,
                "exogenous_inflow_rate": p.exogenous_inflow_rate,
                "operating_burn_rate": p.operating_burn_rate,
                "payment_discipline": p.payment_discipline,
                "stress_threshold_ratio": p.stress_threshold_ratio,
                "systemic_weight": p.systemic_weight,
                "initial_buffer": p.initial_buffer,
                "layer": p.metadata.get("layer"),
                "throughput": p.metadata.get("throughput"),
                "margin": p.metadata.get("margin"),
            }
            for p in graph.merchants.values()
        ]
    )


def payment_rows(events: Iterable[PaymentEvent]) -> Iterator[dict[str, Any]]:
    for e in events:
        yield {
            "event_id": e.event_id,
            "payer_id": e.payer_id,
            "payee_id": e.payee_id,
            "amount": e.amount,
            "t": e.t,
            "obligation_id": e.obligation_id,
            "channel": str(e.channel),
            "status": str(e.status),
            "settlement_lag_hours": e.settlement_lag_hours,
            "external_id": e.external_id,
            "is_synthetic": e.is_synthetic,
            "driver": e.metadata.get("driver"),
        }


def payments_frame(graph: TemporalPaymentGraph) -> pd.DataFrame:
    return pd.DataFrame(list(payment_rows(graph.payment_events)))


def obligations_frame(graph: TemporalPaymentGraph) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "obligation_id": o.obligation_id,
                "debtor_id": o.debtor_id,
                "creditor_id": o.creditor_id,
                "amount": o.amount,
                "amount_paid": o.amount_paid,
                "issued_t": o.issued_t,
                "due_t": o.due_t,
                "original_due_t": o.original_due_t,
                "settled_t": o.settled_t,
                "kind": str(o.kind),
                "status": str(o.status),
                "priority": o.priority,
                "parent_obligation_id": o.parent_obligation_id,
                "external_id": o.external_id,
            }
            for o in graph.obligations
        ]
    )


def edges_frame(graph: TemporalPaymentGraph) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source_id": e.source_id,
                "target_id": e.target_id,
                "pass_through": e.pass_through,
                "conditional_probability": e.conditional_probability,
                "reliability": e.reliability,
                "excitation_alpha": e.excitation_alpha,
                "excitation_decay": e.excitation_decay,
                "base_intensity": e.base_intensity,
                "lag_mu_log": e.lag.mu_log,
                "lag_sigma_log": e.lag.sigma_log,
                "lag_floor_hours": e.lag.floor_hours,
                "lag_max_hours": e.lag.max_hours,
                "lag_mean_hours": e.lag.mean_hours,
                "n_events": e.features.n_events,
                "mean_amount": e.features.mean_amount,
                "recurrence": str(e.features.recurrence),
                "period_hours": e.features.period_hours,
                "regularity": e.features.regularity,
                "is_ground_truth": e.is_ground_truth,
                "estimator": e.estimator,
                "confidence": e.confidence,
                "horizon_flow": e.metadata.get("horizon_flow"),
            }
            for e in graph.dependency_edges
        ]
    )


# ------------------------------------------------------------------- writing


def _write(frame: pd.DataFrame, base: Path, fmt: ExportFormat) -> tuple[str, int]:
    path = base.with_suffix(".parquet" if fmt == "parquet" else ".csv")
    if fmt == "parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)
    return path.name, len(frame)


def stream_payments_to_parquet(
    events: Iterable[PaymentEvent],
    path: Path,
    *,
    batch_rows: int = STREAM_BATCH_ROWS,
) -> int:
    """Write the payment table in batches without materialising it all.

    Returns the row count. Used for LARGE datasets, where holding the full frame
    alongside the graph is the single biggest memory cost of an export.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer: pq.ParquetWriter | None = None
    batch: list[dict[str, Any]] = []
    total = 0

    def flush() -> None:
        nonlocal writer, batch
        if not batch:
            return
        table = pa.Table.from_pylist(batch)
        if writer is None:
            writer = pq.ParquetWriter(path, table.schema)
        writer.write_table(table)
        batch = []

    try:
        for row in payment_rows(events):
            batch.append(row)
            total += 1
            if len(batch) >= batch_rows:
                flush()
        flush()
    finally:
        if writer is not None:
            writer.close()

    if total == 0:
        # An empty table still needs a file with the right schema, or the loader
        # reports a missing dataset rather than an empty one.
        pd.DataFrame(columns=list(PAYMENT_COLUMNS)).to_parquet(path, index=False)
    return total


def export_dataset(
    network: SyntheticNetwork,
    root: Path,
    *,
    fmt: ExportFormat = "parquet",
    scale: str | None = None,
    streaming: bool = False,
    include_ground_truth: bool = True,
) -> ExportResult:
    """Write a dataset directory. Returns what was written."""
    directory = Path(root) / network.dataset_version
    directory.mkdir(parents=True, exist_ok=True)
    graph = network.graph

    files: dict[str, str] = {}
    rows: dict[str, int] = {}

    name, count = _write(merchants_frame(graph), directory / MERCHANTS_FILE, fmt)
    files["merchants"], rows["merchants"] = name, count

    if streaming and fmt == "parquet":
        path = directory / f"{PAYMENTS_FILE}.parquet"
        count = stream_payments_to_parquet(graph.payment_events, path)
        files["payments"], rows["payments"] = path.name, count
    else:
        name, count = _write(payments_frame(graph), directory / PAYMENTS_FILE, fmt)
        files["payments"], rows["payments"] = name, count

    name, count = _write(obligations_frame(graph), directory / OBLIGATIONS_FILE, fmt)
    files["obligations"], rows["obligations"] = name, count

    if include_ground_truth:
        name, count = _write(edges_frame(graph), directory / EDGES_FILE, fmt)
        files["dependency_edges"], rows["dependency_edges"] = name, count

    manifest = DatasetManifest.for_config(
        network.config,
        seeds=network.seeds,
        scale=scale,
        stats={**network.stats, **network.graph.stats().to_dict(), "rows": rows},
    )
    manifest.save(directory)
    files["manifest"] = "manifest.json"

    logger.info(
        "dataset_exported",
        dataset_id=network.dataset_version,
        directory=str(directory),
        fmt=fmt,
        **rows,
    )
    return ExportResult(directory=directory, fmt=fmt, files=files, rows=rows)


def export_scenario(
    directory: Path,
    scenario: Any,
    ground_truth: Any = None,
) -> Path:
    """Write one scenario (and optionally its ground truth) under a dataset dir."""
    target = Path(directory) / SCENARIOS_DIR / scenario.scenario_id
    target.mkdir(parents=True, exist_ok=True)

    (target / "scenario.json").write_text(
        json.dumps(scenario.to_dict(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    if ground_truth is not None:
        (target / "ground_truth.json").write_text(
            json.dumps(ground_truth.to_dict(), indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    return target


# ------------------------------------------------------------------- reading


def _read(base: Path) -> pd.DataFrame:
    parquet, csv = base.with_suffix(".parquet"), base.with_suffix(".csv")
    if parquet.exists():
        return pd.read_parquet(parquet)
    if csv.exists():
        return pd.read_csv(csv)
    raise NotFoundError(f"no parquet or csv table at {base}")


def load_dataset(
    directory: Path, *, with_ground_truth: bool = False
) -> tuple[TemporalPaymentGraph, DatasetManifest]:
    """Rehydrate an exported dataset.

    ``with_ground_truth=False`` (the default) deliberately leaves the dependency
    overlay empty. A model consuming a loaded dataset must infer dependencies
    from the payment stream; loading the generator's edges by default would make
    that impossible to enforce.
    """
    directory = Path(directory)
    if not directory.exists():
        raise NotFoundError(f"no dataset directory at {directory}")

    manifest = DatasetManifest.load(directory)
    merchants = _read(directory / MERCHANTS_FILE)
    payments = _read(directory / PAYMENTS_FILE)
    obligations = _read(directory / OBLIGATIONS_FILE)

    profiles = [
        MerchantProfile(
            merchant_id=row.merchant_id,
            external_id=_opt_str(row.external_id),
            name=str(row.name),
            sector=MerchantSector(row.sector),
            tier=MerchantTier(row.tier),
            opening_balance=float(row.opening_balance),
            operating_floor=float(row.operating_floor),
            credit_limit=float(row.credit_limit),
            exogenous_inflow_rate=float(row.exogenous_inflow_rate),
            operating_burn_rate=float(row.operating_burn_rate),
            payment_discipline=float(row.payment_discipline),
            stress_threshold_ratio=float(row.stress_threshold_ratio),
            systemic_weight=float(row.systemic_weight),
            metadata={
                "layer": _opt_int(row.layer),
                "throughput": _opt_float(row.throughput),
                "margin": _opt_float(row.margin),
            },
        )
        for row in merchants.itertuples(index=False)
    ]

    events = [
        PaymentEvent(
            event_id=row.event_id,
            payer_id=row.payer_id,
            payee_id=row.payee_id,
            amount=float(row.amount),
            t=float(row.t),
            obligation_id=_opt_str(row.obligation_id),
            channel=PaymentChannel(row.channel),
            status=PaymentStatus(row.status),
            settlement_lag_hours=float(row.settlement_lag_hours),
            external_id=_opt_str(row.external_id),
            is_synthetic=bool(row.is_synthetic),
            metadata={"driver": _opt_str(row.driver)},
        )
        for row in payments.itertuples(index=False)
    ]

    commitments = [
        Obligation(
            obligation_id=row.obligation_id,
            debtor_id=row.debtor_id,
            creditor_id=row.creditor_id,
            amount=float(row.amount),
            amount_paid=float(row.amount_paid),
            issued_t=float(row.issued_t),
            due_t=float(row.due_t),
            original_due_t=_opt_float(row.original_due_t),
            settled_t=_opt_float(row.settled_t),
            kind=ObligationKind(row.kind),
            status=ObligationStatus(row.status),
            priority=int(row.priority),
            parent_obligation_id=_opt_str(row.parent_obligation_id),
            external_id=_opt_str(row.external_id),
        )
        for row in obligations.itertuples(index=False)
    ]

    edges: list[DependencyEdge] = []
    if with_ground_truth:
        frame = _read(directory / EDGES_FILE)
        edges = [
            DependencyEdge(
                source_id=row.source_id,
                target_id=row.target_id,
                pass_through=float(row.pass_through),
                conditional_probability=float(row.conditional_probability),
                reliability=float(row.reliability),
                excitation_alpha=float(row.excitation_alpha),
                excitation_decay=float(row.excitation_decay),
                base_intensity=float(row.base_intensity),
                lag=LagDistribution(
                    mu_log=float(row.lag_mu_log),
                    sigma_log=float(row.lag_sigma_log),
                    floor_hours=float(row.lag_floor_hours),
                    max_hours=float(row.lag_max_hours),
                ),
                is_ground_truth=bool(row.is_ground_truth),
                estimator=_opt_str(row.estimator),
                confidence=float(row.confidence),
                metadata={"horizon_flow": _opt_float(row.horizon_flow)},
            )
            for row in frame.itertuples(index=False)
        ]

    graph = build_graph(
        profiles,
        events,
        commitments,
        edges,
        network_id=manifest.dataset_id,
        dataset_version=manifest.dataset_id,
    )
    logger.info(
        "dataset_loaded",
        dataset_id=manifest.dataset_id,
        n_merchants=len(profiles),
        n_events=len(events),
        with_ground_truth=with_ground_truth,
    )
    return graph, manifest


def load_scenario_ground_truth(directory: Path, scenario_id: str) -> dict[str, Any]:
    path = Path(directory) / SCENARIOS_DIR / scenario_id / "ground_truth.json"
    if not path.exists():
        raise NotFoundError(f"no ground truth for scenario {scenario_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_scenarios(directory: Path) -> list[str]:
    base = Path(directory) / SCENARIOS_DIR
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def _opt_str(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value)
    return None if text in ("nan", "None", "") else text


def _opt_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def replay_scenario(
    directory: Path,
    scenario_id: str,
    *,
    with_ground_truth: bool = True,
) -> dict[str, Any]:
    """Deterministically rebuild a scenario from its exported manifest.

    Regenerates the network from the manifest's parameters and seed, rebuilds
    the scenario from its recorded spec, and re-derives the ground truth. The
    result is compared against what was stored, so a replay that *differs* is
    reported rather than silently accepted - which is the only way a
    reproducibility claim can be checked instead of asserted.
    """
    from lce.benchmark.ground_truth import compute_ground_truth
    from lce.benchmark.scenarios import ScenarioFamily, ScenarioSpec, TargetStrategy, build_scenario
    from lce.data.generator import generate_network
    from lce.simulation.engine import SimulationConfig

    directory = Path(directory)
    manifest = DatasetManifest.load(directory)
    manifest.verify()

    spec_path = directory / SCENARIOS_DIR / scenario_id / "scenario.json"
    if not spec_path.exists():
        raise NotFoundError(f"no scenario {scenario_id!r} under {directory}")
    stored = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_payload = stored["spec"]

    spec = ScenarioSpec(
        family=ScenarioFamily(spec_payload["family"]),
        magnitude=float(spec_payload["magnitude"]),
        shock_time=(
            None
            if spec_payload.get("shock_time") is None
            else float(spec_payload["shock_time"])
        ),
        delay_hours=float(spec_payload["delay_hours"]),
        partial_fraction=float(spec_payload["partial_fraction"]),
        n_targets=int(spec_payload["n_targets"]),
        target_strategy=TargetStrategy(spec_payload["target_strategy"]),
        explicit_targets=tuple(spec_payload.get("explicit_targets", [])),
        seed=int(spec_payload["seed"]),
    )

    network = generate_network(manifest.rebuild_config())
    scenario = build_scenario(network.graph, spec, dataset_id=network.dataset_version)

    result: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "dataset_id": network.dataset_version,
        "dataset_matches": network.dataset_version == manifest.dataset_id,
        "scenario_id_matches": scenario.scenario_id == scenario_id,
        "targets": scenario.targets,
        "targets_match": scenario.targets == stored.get("targets"),
    }

    if with_ground_truth:
        sim_config = SimulationConfig(
            horizon_hours=network.config.horizon_hours, seed=network.config.seed
        )
        truth = compute_ground_truth(
            scenario,
            true_edges=network.ground_truth_edges,
            config=sim_config,
            compute_optimum=False,
        )
        result["ground_truth"] = truth.summary()
        gt_path = directory / SCENARIOS_DIR / scenario_id / "ground_truth.json"
        if gt_path.exists():
            original = json.loads(gt_path.read_text(encoding="utf-8"))
            result["affected_matches"] = (
                sorted(truth.affected_nodes)
                == sorted(original.get("cascade", {}).get("affected_nodes", []))
            )
            result["depth_matches"] = (
                truth.max_cascade_depth
                == original.get("cascade", {}).get("max_cascade_depth")
            )

    mismatches = [k for k, v in result.items() if k.endswith("matches") and v is False]
    result["reproduced"] = not mismatches
    if mismatches:
        raise ValidationError(
            f"replay of {scenario_id!r} did not reproduce the stored scenario: "
            f"{mismatches}",
            mismatches=mismatches,
        )
    return result
