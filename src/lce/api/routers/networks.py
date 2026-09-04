"""Dataset, merchant and graph endpoints."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, HTTPException, Query, status

from lce.api.deps import Analysis, Config, Networks
from lce.api.schemas import (
    DatasetDetail,
    DatasetSummary,
    EdgeView,
    GenerateNetworkRequest,
    GraphView,
    LearnDependenciesRequest,
    MerchantDetail,
    MerchantSummary,
    PagedMerchants,
)
from lce.errors import NotFoundError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.models.dependency import DependencyLearnerConfig

router = APIRouter(tags=["network"])


def _merchant_summary(graph: TemporalPaymentGraph, merchant_id: str) -> MerchantSummary:
    profile = graph.merchant(merchant_id)
    return MerchantSummary(
        merchant_id=profile.merchant_id,
        name=profile.name,
        sector=str(profile.sector),
        tier=str(profile.tier),
        opening_balance=profile.opening_balance,
        operating_floor=profile.operating_floor,
        credit_limit=profile.credit_limit,
        initial_buffer=profile.initial_buffer,
        systemic_weight=profile.systemic_weight,
        payment_discipline=profile.payment_discipline,
    )


@router.post(
    "/datasets",
    response_model=DatasetDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Generate and store a synthetic merchant network",
)
def generate_dataset(
    request: GenerateNetworkRequest, networks: Networks, settings: Config
) -> DatasetDetail:
    if request.coverage_low > request.coverage_high:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="coverage_low must not exceed coverage_high",
        )

    from lce.data.generator import GeneratorConfig

    config = replace(
        GeneratorConfig(),
        n_merchants=request.n_merchants,
        n_layers=request.n_layers,
        seed=request.seed if request.seed is not None else settings.random_seed,
        horizon_hours=request.horizon_hours,
        history_hours=request.history_hours,
        mean_out_degree=request.mean_out_degree,
        coverage_low=request.coverage_low,
        coverage_high=request.coverage_high,
    )
    try:
        network = networks.generate_and_store(config, notes=request.notes)
    except ValueError as exc:
        # Datasets are content-addressed, so an identical request is a conflict
        # rather than a new dataset.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return DatasetDetail(**networks.dataset_summary(network.dataset_version))


@router.get("/datasets", response_model=list[DatasetSummary], summary="List datasets")
def list_datasets(
    networks: Networks, limit: int = Query(default=25, ge=1, le=200)
) -> list[DatasetSummary]:
    return [DatasetSummary(**row) for row in networks.list_datasets(limit)]


@router.get(
    "/datasets/{dataset_id}", response_model=DatasetDetail, summary="Dataset detail"
)
def get_dataset(dataset_id: str, networks: Networks) -> DatasetDetail:
    return DatasetDetail(**networks.dataset_summary(dataset_id))


@router.get(
    "/datasets/{dataset_id}/merchants",
    response_model=PagedMerchants,
    summary="List merchants in a dataset",
)
def list_merchants(
    dataset_id: str,
    networks: Networks,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sector: str | None = None,
    tier: str | None = None,
) -> PagedMerchants:
    profiles = networks.uow.merchants.list_for_dataset(
        dataset_id, sector=sector, tier=tier, limit=limit, offset=offset
    )
    total = networks.uow.merchants.count_for_dataset(dataset_id)
    if total == 0:
        raise NotFoundError(f"unknown dataset {dataset_id!r}", dataset_id=dataset_id)

    return PagedMerchants(
        items=[
            MerchantSummary(
                merchant_id=p.merchant_id,
                name=p.name,
                sector=str(p.sector),
                tier=str(p.tier),
                opening_balance=p.opening_balance,
                operating_floor=p.operating_floor,
                credit_limit=p.credit_limit,
                initial_buffer=p.initial_buffer,
                systemic_weight=p.systemic_weight,
                payment_discipline=p.payment_discipline,
            )
            for p in profiles
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/datasets/{dataset_id}/merchants/{merchant_id}",
    response_model=MerchantDetail,
    summary="Merchant detail with its network position",
)
def get_merchant(dataset_id: str, merchant_id: str, networks: Networks) -> MerchantDetail:
    graph = networks.load_graph(dataset_id)
    if not graph.has_merchant(merchant_id):
        raise NotFoundError(f"unknown merchant {merchant_id!r}", merchant_id=merchant_id)

    profile = graph.merchant(merchant_id)
    autonomy = profile.autonomy_hours()
    payables = sum(o.outstanding for o in graph.payables_of(merchant_id) if o.is_open)
    receivables = sum(o.outstanding for o in graph.receivables_of(merchant_id) if o.is_open)

    return MerchantDetail(
        **_merchant_summary(graph, merchant_id).model_dump(),
        exogenous_inflow_rate=profile.exogenous_inflow_rate,
        operating_burn_rate=profile.operating_burn_rate,
        autonomy_hours=autonomy if autonomy != float("inf") else None,
        n_suppliers=len(graph.successors(merchant_id)),
        n_buyers=len(graph.predecessors(merchant_id)),
        payables_in_horizon=payables,
        receivables_in_horizon=receivables,
        metadata=profile.metadata,
    )


@router.get(
    "/datasets/{dataset_id}/graph",
    response_model=GraphView,
    summary="Dependency graph view",
)
def get_graph(
    dataset_id: str,
    networks: Networks,
    estimator: str | None = Query(default=None),
    max_edges: int = Query(default=500, ge=1, le=10000),
    min_pass_through: float = Query(default=0.0, ge=0.0, le=1.0),
) -> GraphView:
    graph = networks.load_graph(dataset_id, estimator=estimator)
    edges = [
        e for e in graph.dependency_edges if e.pass_through >= min_pass_through
    ]
    # Strongest links first, so truncation keeps the structurally important ones.
    edges.sort(key=lambda e: -e.pass_through)
    edges = edges[:max_edges]

    return GraphView(
        dataset_id=dataset_id,
        stats=graph.stats().to_dict(),
        nodes=[_merchant_summary(graph, m) for m in graph.merchant_ids],
        edges=[
            EdgeView(
                source_id=e.source_id,
                target_id=e.target_id,
                pass_through=e.pass_through,
                conditional_probability=e.conditional_probability,
                reliability=e.reliability,
                mean_lag_hours=e.lag.mean_hours,
                n_events=e.features.n_events,
                mean_amount=e.features.mean_amount,
                estimator=e.estimator,
                is_ground_truth=e.is_ground_truth,
                confidence=e.confidence,
            )
            for e in edges
        ],
    )


@router.post(
    "/datasets/{dataset_id}/dependencies",
    summary="Learn the dependency overlay from the event history",
)
def learn_dependencies(
    dataset_id: str, request: LearnDependenciesRequest, analysis: Analysis
) -> dict[str, object]:
    """Fit conditional dependencies from observed payments only.

    The default ``t_end=0.0`` uses the historical window and holds the entire
    simulation horizon out, so a subsequent evaluation is not scored on data the
    learner already saw.
    """
    config = replace(
        DependencyLearnerConfig(),
        em_iterations=request.em_iterations,
        max_parents_per_event=request.max_parents_per_event,
    )
    return analysis.learn_dependencies(dataset_id, config, t_end=request.t_end)
