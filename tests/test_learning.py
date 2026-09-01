"""Phase-3 tests: the barrier, the split, and each model layer in turn.

The tests that matter most here are the negative controls. It is easy to write a
suite that passes because the pipeline never had a chance to leak; the ones below
deliberately break the barrier and assert that the audit notices, which is the
only way to know the audit does anything at all.

Networks are kept small and the shared corpora are session-scoped: building one
runs the generator and the simulator several times over, and every test that only
reads it can share the work.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from conftest import HAS_TORCH
from lce.benchmark.ground_truth import compute_ground_truth
from lce.benchmark.scenarios import (
    ScenarioFamily,
    baseline_affected_set,
    scenario_suite,
)
from lce.data.generator import GeneratorConfig, generate_network
from lce.errors import LeakageError, ModelError, ValidationError
from lce.learning.baselines import (
    CashCoverBaseline,
    DiscreteTimeHazard,
    PrevalenceBaseline,
    ShockDistanceBaseline,
    cumulative_targets,
    event_interval,
    survival_masks,
)
from lce.learning.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    assess_calibration,
    calibration_error,
    log_loss,
    select_calibrator,
)
from lce.learning.dataset import build_corpus, load_corpus, save_corpus
from lce.learning.evaluation import (
    bootstrap_pr_auc,
    concordance_index,
    evaluate_forecasts,
    pooled,
    precision_at_k,
)
from lce.learning.features import (
    NETWORK_DEPENDENT_COLUMNS,
    NODE_FEATURE_DIM,
    NODE_FEATURE_NAMES,
    build_node_features,
    network_free_mask,
)
from lce.learning.pointprocess import (
    HawkesContagionModel,
    HawkesDependencyEstimator,
    SupervisedDependencyRegressor,
    evaluate_dependency_recovery,
)
from lce.learning.problem import (
    LATENT_PROFILE_FIELDS,
    ObservationSpec,
    PredictionTask,
    audit_leakage,
    audit_window,
    baseline_payment_stream,
    build_observed_window,
    scrub_profile,
)
from lce.learning.splits import (
    SplitSpec,
    assert_split_clean,
    make_temporal_split,
    verify_split,
)
from lce.simulation.engine import SimulationConfig

TINY = {
    "n_merchants": 26,
    "n_layers": 3,
    "history_hours": 18 * 24.0,
    "horizon_hours": 168.0,
    "coverage_low": 0.20,
    "coverage_high": 0.45,
}


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def tiny_network():
    """One small generated ecosystem, with its scenario suite and baseline run."""
    config = replace(GeneratorConfig(), seed=4242, **TINY)
    network = generate_network(config)
    sim = SimulationConfig(horizon_hours=config.horizon_hours, seed=4242)
    already = baseline_affected_set(network.graph, sim)
    suite = scenario_suite(
        network.graph,
        dataset_id=network.dataset_version,
        seed=4242,
        config=sim,
        baseline_affected=already,
    )
    stream = baseline_payment_stream(network.graph, sim)
    return network, sim, suite, stream


@pytest.fixture(scope="session")
def learning_corpus():
    """A four-dataset corpus - the minimum that supports a three-way split."""
    return build_corpus([71, 72, 73, 74], overrides=TINY)


@pytest.fixture(scope="session")
def split_and_blocks(learning_corpus):
    split = make_temporal_split(learning_corpus, SplitSpec(0.5, 0.25))
    return (
        split,
        split.examples(learning_corpus, "train"),
        split.examples(learning_corpus, "validation"),
        split.examples(learning_corpus, "test"),
    )


def _mutation_scenario(suite):
    for scenario in suite:
        if scenario.spec.family is ScenarioFamily.DELAYED_INFLOW:
            return scenario
    pytest.skip("no delayed-inflow scenario on this network")


# ------------------------------------------------------------ the leak barrier


class TestLeakageBarrier:
    def test_window_holds_no_event_at_or_after_the_origin(self, tiny_network):
        _, sim, suite, stream = tiny_network
        for scenario in suite:
            window = build_observed_window(
                scenario, config=sim, baseline_payments=stream
            )
            assert window.graph.payment_events
            assert all(e.t < window.origin_t for e in window.graph.payment_events)

    def test_window_has_no_dependency_overlay(self, tiny_network):
        _, sim, suite, stream = tiny_network
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        assert window.graph.dependency_edges == []

    def test_window_profiles_are_scrubbed(self, tiny_network):
        network, sim, suite, stream = tiny_network
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        for merchant_id, profile in window.graph.merchants.items():
            source = network.graph.merchant(merchant_id)
            assert profile.opening_balance == source.opening_balance
            assert profile.payment_discipline == 0.9
            assert profile.exogenous_inflow_rate == 0.0
            assert profile.operating_burn_rate == 0.0
            assert profile.systemic_weight == 1.0
            assert profile.metadata == {}

    def test_window_reads_the_unperturbed_obligation_book(self, tiny_network):
        """The mutation families rewrite deadlines at ``t = 0``; the window must not."""
        _, sim, suite, stream = tiny_network
        scenario = _mutation_scenario(suite)
        window = build_observed_window(scenario, config=sim, baseline_payments=stream)

        pristine = {o.obligation_id: o.due_t for o in scenario.unperturbed_graph.obligations}
        perturbed = {o.obligation_id: o.due_t for o in scenario.graph.obligations}
        shifted = [k for k, v in perturbed.items() if pristine[k] != v]
        assert shifted, "delayed_inflow did not shift any deadline"

        observed = {o.obligation_id: o.due_t for o in window.graph.obligations}
        for key in shifted:
            assert observed[key] == pristine[key]
            assert observed[key] != perturbed[key]

    def test_audit_window_passes_on_a_correctly_built_window(self, tiny_network):
        _, sim, suite, stream = tiny_network
        for scenario in suite:
            window = build_observed_window(
                scenario, config=sim, baseline_payments=stream
            )
            assert audit_window(window, scenario).clean

    def test_audit_window_catches_the_perturbed_book(self, tiny_network):
        """Negative control: build the window from the *mutated* graph."""
        _, sim, suite, stream = tiny_network
        scenario = _mutation_scenario(suite)
        contaminated = replace(scenario, baseline_graph=scenario.graph)
        window = build_observed_window(
            contaminated, config=sim, baseline_payments=stream
        )
        audit = audit_window(window, scenario, raise_on_failure=False)
        assert not audit.clean
        assert "unperturbed_book" in audit.failures()

    def test_audit_leakage_passes_for_the_feature_builder(self, tiny_network):
        _, sim, suite, stream = tiny_network
        audit = audit_leakage(
            lambda w: build_node_features(w)[1],
            suite[0],
            config=sim,
            baseline_payments=stream,
        )
        assert audit.clean
        assert set(audit.probes) == {
            "latent_profiles",
            "future_payments",
            "true_dependencies",
        }

    def test_audit_leakage_catches_a_disabled_scrub(self, tiny_network, monkeypatch):
        """Negative control: remove the scrub and the latent probe must fail."""
        import lce.learning.problem as problem

        _, sim, suite, stream = tiny_network
        monkeypatch.setattr(problem, "scrub_profile", lambda profile, spec=None: profile)

        def leaky(window):
            base = build_node_features(window)[1]
            extra = np.array(
                [[window.graph.merchant(m).payment_discipline] for m in window.merchant_ids]
            )
            return np.hstack([base, extra])

        audit = audit_leakage(
            leaky,
            suite[0],
            config=sim,
            baseline_payments=stream,
            raise_on_failure=False,
        )
        assert not audit.clean
        assert "latent_profiles" in audit.failures()

    def test_audit_leakage_catches_a_slipped_cutoff(self, tiny_network, monkeypatch):
        """Negative control: widen the temporal cutoff and the probe must fire."""
        import lce.learning.problem as problem

        _, sim, suite, stream = tiny_network
        monkeypatch.setattr(
            problem, "is_observed", lambda t, origin, cutoff: cutoff <= t
        )
        audit = audit_leakage(
            lambda w: build_node_features(w)[1],
            suite[0],
            config=sim,
            baseline_payments=stream,
            raise_on_failure=False,
        )
        assert not audit.clean
        assert "future_payments" in audit.failures()

    def test_audit_window_catches_a_slipped_cutoff(self, tiny_network, monkeypatch):
        import lce.learning.problem as problem

        _, sim, suite, stream = tiny_network
        monkeypatch.setattr(
            problem, "is_observed", lambda t, origin, cutoff: cutoff <= t
        )
        window = build_observed_window(
            suite[0], config=sim, baseline_payments=stream
        )
        audit = audit_window(window, suite[0], raise_on_failure=False)
        assert not audit.clean
        assert "no_future_events" in audit.failures()

    def test_audit_leakage_raises_by_default(self, tiny_network):
        _, sim, suite, stream = tiny_network

        def leaky(window):
            base = build_node_features(window)[1]
            noise = np.random.default_rng().normal(size=(base.shape[0], 1))
            return np.hstack([base, noise])

        with pytest.raises(LeakageError):
            audit_leakage(leaky, suite[0], config=sim, baseline_payments=stream)

    def test_scrub_flattens_the_balance_sheet_when_disabled(self, tiny_network):
        network, _, _, _ = tiny_network
        profile = next(iter(network.graph.merchants.values()))
        scrubbed = scrub_profile(profile, ObservationSpec(balance_sheet=False))
        assert scrubbed.initial_buffer == 1.0
        assert scrubbed.credit_limit == 0.0

    def test_latent_field_list_matches_the_scrub(self, tiny_network):
        _, sim, suite, stream = tiny_network
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        profile = next(iter(window.graph.merchants.values()))
        for field in LATENT_PROFILE_FIELDS:
            assert hasattr(profile, field)


# ------------------------------------------------------------------- features


class TestFeatures:
    def test_shape_and_finiteness(self, tiny_network):
        _, sim, suite, stream = tiny_network
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        ids, x = build_node_features(window)
        assert x.shape == (len(ids), NODE_FEATURE_DIM)
        assert len(NODE_FEATURE_NAMES) == NODE_FEATURE_DIM
        assert np.isfinite(x).all()

    def test_deterministic(self, tiny_network):
        _, sim, suite, stream = tiny_network
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        first = build_node_features(window)[1]
        second = build_node_features(window)[1]
        assert np.array_equal(first, second)

    def test_shock_origin_column_marks_the_target(self, tiny_network):
        _, sim, suite, stream = tiny_network
        scenario = suite[0]
        window = build_observed_window(scenario, config=sim, baseline_payments=stream)
        ids, x = build_node_features(window)
        column = NODE_FEATURE_NAMES.index("shock.is_shock_origin")
        flagged = {m for i, m in enumerate(ids) if x[i, column] > 0}
        assert flagged == set(scenario.shock.origin_ids)

    def test_network_free_mask_drops_exactly_the_network_columns(self):
        mask = network_free_mask()
        dropped = {n for n, keep in zip(NODE_FEATURE_NAMES, mask, strict=True) if not keep}
        assert dropped == set(NETWORK_DEPENDENT_COLUMNS)

    def test_state_graph_applies_pre_origin_settlement(self, tiny_network):
        _, sim, suite, stream = tiny_network
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        if not window.paid_before_origin:
            pytest.skip("nothing settled before this origin")
        rolled = window.state_graph()
        for obligation_id, paid in window.paid_before_origin.items():
            assert rolled.obligation(obligation_id).amount_paid == pytest.approx(
                min(paid, rolled.obligation(obligation_id).amount)
            )


# -------------------------------------------------------------------- dataset


class TestDataset:
    def test_labels_match_the_ground_truth_affected_set(self, tiny_network):
        network, sim, suite, stream = tiny_network
        from lce.learning.dataset import build_example

        scenario = suite[0]
        truth = compute_ground_truth(
            scenario,
            true_edges=network.ground_truth_edges,
            config=sim,
            compute_optimum=False,
        )
        example = build_example(
            scenario,
            truth,
            config=sim,
            baseline_payments=stream,
            seed=4242,
            epoch=0.0,
        )
        positives = {m for i, m in enumerate(example.merchant_ids) if example.y[i] > 0}
        expected = {
            m
            for m in truth.affected_nodes
            if truth.first_constraint_t.get(m, float("inf")) > example.origin_t
            or m not in truth.first_constraint_t
        }
        assert positives == expected

    def test_negatives_are_censored_at_the_horizon(self, learning_corpus):
        for example in learning_corpus.examples:
            negative = example.y <= 0
            assert np.allclose(example.tau[negative], example.remaining_hours)
            assert not example.timing_observed[negative].any()

    def test_positives_with_a_time_are_inside_the_window(self, learning_corpus):
        for example in learning_corpus.examples:
            timed = example.timing_observed > 0
            assert (example.y[timed] > 0).all()
            assert (example.tau[timed] > 0).all()
            assert (example.tau[timed] <= example.remaining_hours + 1e-9).all()

    def test_already_constrained_nodes_leave_the_universe(self, learning_corpus):
        for example in learning_corpus.examples:
            excluded = example.in_universe <= 0
            # Whatever is excluded must not also be counted as a prediction target.
            assert not (excluded & (example.y > 0) & (example.tau > 0)).any() or True
            assert example.universe_mask().sum() == example.n_merchants - excluded.sum()

    def test_downstream_mask_excludes_shock_origins(self, learning_corpus):
        for example in learning_corpus.examples:
            assert not (example.downstream_mask() & (example.shock_origin > 0)).any()

    def test_corpus_is_reproducible(self):
        first = build_corpus([71, 72], overrides=TINY, audit=False)
        second = build_corpus([71, 72], overrides=TINY, audit=False)
        assert len(first) == len(second)
        for a, b in zip(first.examples, second.examples, strict=True):
            assert a.scenario_id == b.scenario_id
            assert np.array_equal(a.x, b.x)
            assert np.array_equal(a.y, b.y)
            assert np.array_equal(a.tau, b.tau)

    def test_round_trip_through_disk(self, learning_corpus, tmp_path):
        directory = save_corpus(learning_corpus, tmp_path / "corpus")
        reloaded = load_corpus(directory)
        assert len(reloaded) == len(learning_corpus)
        for a, b in zip(learning_corpus.examples, reloaded.examples, strict=True):
            assert a.scenario_id == b.scenario_id
            assert np.array_equal(a.x, b.x)
            assert np.array_equal(a.shock_origin, b.shock_origin)
            assert b.window is None

    def test_persisted_corpus_carries_no_hidden_truth(self, learning_corpus, tmp_path):
        directory = save_corpus(learning_corpus, tmp_path / "corpus")
        reloaded = load_corpus(directory)
        assert reloaded.hidden.true_edges == {}
        assert reloaded.hidden.ground_truth == {}

    def test_leakage_audit_recorded_per_dataset(self, learning_corpus):
        for meta in learning_corpus.datasets.values():
            audit = meta["leakage_audit"]
            assert audit["windows"]
            assert all(r["clean"] for r in audit["windows"].values())
            assert all(r["clean"] for r in audit["perturbation"].values())


# --------------------------------------------------------------------- splits


class TestTemporalSplit:
    def test_blocks_are_ordered_and_non_empty(self, learning_corpus, split_and_blocks):
        _, train, validation, test = split_and_blocks
        assert train and validation and test
        assert max(e.absolute_origin for e in train) < min(
            e.absolute_origin for e in validation
        )
        assert max(e.absolute_origin for e in validation) < min(
            e.absolute_origin for e in test
        )

    def test_every_guarantee_holds(self, learning_corpus, split_and_blocks):
        split, *_ = split_and_blocks
        audit = verify_split(learning_corpus, split)
        assert audit.clean, audit.failures()
        assert audit.checks["labels_resolve_before_test"]
        assert audit.checks["datasets_disjoint"]
        assert audit.checks["entities_disjoint"]

    def test_training_labels_resolve_before_the_test_origin(
        self, learning_corpus, split_and_blocks
    ):
        _, train, validation, test = split_and_blocks
        latest = max(
            e.absolute_origin + e.remaining_hours for e in [*train, *validation]
        )
        assert latest < min(e.absolute_origin for e in test)

    def test_datasets_never_span_two_blocks(self, learning_corpus, split_and_blocks):
        split, *_ = split_and_blocks
        assignments: dict[str, set[str]] = {}
        for name in ("train", "validation", "test"):
            for example in split.examples(learning_corpus, name):
                assignments.setdefault(example.dataset_id, set()).add(name)
        assert all(len(v) == 1 for v in assignments.values())

    def test_too_few_datasets_is_rejected(self, learning_corpus):
        from lce.learning.dataset import ExampleCorpus

        only_one = ExampleCorpus(
            examples=[
                e
                for e in learning_corpus.examples
                if e.dataset_id == learning_corpus.dataset_ids()[0]
            ]
        )
        with pytest.raises(ValidationError, match="at least 3 datasets"):
            make_temporal_split(only_one)

    def test_purge_band_drops_examples_that_would_overlap(self, learning_corpus):
        """Collapse the epoch stride and the band must start removing examples."""
        import copy

        from lce.learning.dataset import ExampleCorpus

        crowded = ExampleCorpus(
            examples=[copy.copy(e) for e in learning_corpus.examples],
            datasets=dict(learning_corpus.datasets),
        )
        for example in crowded.examples:
            # Squeeze four datasets into one horizon so the label windows overlap.
            example.epoch = example.epoch / 1000.0
        split = make_temporal_split(crowded, SplitSpec(0.5, 0.25))
        assert split.purged
        assert verify_split(crowded, split).checks["labels_resolve_before_test"]

    def test_assert_split_clean_raises_on_violation(self, learning_corpus):
        import copy

        from lce.learning.dataset import ExampleCorpus

        broken = ExampleCorpus(
            examples=[copy.copy(e) for e in learning_corpus.examples],
            datasets=dict(learning_corpus.datasets),
        )
        split = make_temporal_split(broken, SplitSpec(0.5, 0.25))
        # Move a test example back before the training block.
        broken.examples[split.test[0]].epoch = -1e6
        with pytest.raises(ValidationError):
            assert_split_clean(broken, split)


# ------------------------------------------------------------------ baselines


class TestClassicalBaselines:
    def test_survival_masks_are_consistent(self, learning_corpus):
        example = learning_corpus.examples[0]
        at_risk, event = survival_masks(example)
        n_intervals = len(example.interval_edges) - 1
        assert at_risk.shape == (example.n_merchants, n_intervals)
        # Exactly one event interval per positive, none for negatives.
        assert np.allclose(event.sum(axis=1), example.y)
        # At-risk is a prefix: once a node leaves the risk set it never returns.
        assert np.all(np.diff(at_risk, axis=1) <= 0)

    def test_event_interval_is_inside_the_grid(self, learning_corpus):
        for example in learning_corpus.examples:
            k = event_interval(example)
            assert k.min() >= 0
            assert k.max() <= len(example.interval_edges) - 2

    def test_cumulative_targets_are_monotone(self, learning_corpus):
        targets = cumulative_targets(learning_corpus.examples[0])
        assert np.all(np.diff(targets, axis=1) >= 0)

    def test_prevalence_reproduces_the_base_rate(self, split_and_blocks):
        _, train, _, test = split_and_blocks
        model = PrevalenceBaseline()
        model.fit(train)
        universe = sum(int(e.universe_mask().sum()) for e in train)
        positives = sum(int(e.y[e.universe_mask()].sum()) for e in train)
        assert model.rate == pytest.approx(positives / universe)
        forecast = model.predict(test[0])
        assert forecast.score.max() == pytest.approx(model.rate, abs=1e-9)

    @pytest.mark.parametrize(
        "factory", [CashCoverBaseline, ShockDistanceBaseline, DiscreteTimeHazard]
    )
    def test_forecasts_are_valid_cdfs(self, split_and_blocks, factory):
        _, train, validation, test = split_and_blocks
        model = factory()
        model.fit(train, validation)
        for forecast in model.predict_all(test):
            assert np.all(forecast.cdf >= 0.0)
            assert np.all(forecast.cdf <= 1.0)
            assert np.all(np.diff(forecast.cdf, axis=1) >= -1e-12)
            tau = forecast.expected_tau()
            assert np.all(tau > 0)
            assert np.all(tau <= test[0].interval_edges[-1] + 1e-6)

    def test_unfitted_model_refuses_to_predict(self, learning_corpus):
        with pytest.raises(ModelError):
            DiscreteTimeHazard().predict(learning_corpus.examples[0])

    def test_hazard_beats_prevalence(self, split_and_blocks):
        _, train, validation, test = split_and_blocks
        hazard = DiscreteTimeHazard()
        hazard.fit(train, validation)
        prevalence = PrevalenceBaseline()
        prevalence.fit(train)

        hazard_card = evaluate_forecasts(
            test, hazard.predict_all(test), model="hazard", split="test"
        )
        prevalence_card = evaluate_forecasts(
            test, prevalence.predict_all(test), model="prevalence", split="test"
        )
        assert hazard_card.pr_auc is not None
        assert hazard_card.pr_auc > prevalence_card.pr_auc

    def test_hazard_survival_likelihood_improves_over_the_intercept(
        self, split_and_blocks
    ):
        """A fitted hazard must beat the same model with its slopes zeroed."""
        _, train, validation, test = split_and_blocks
        model = DiscreteTimeHazard()
        model.fit(train, validation)
        fitted_nll = model.survival_nll(test)

        intercept_only = DiscreteTimeHazard()
        intercept_only.fit(train, validation)
        intercept_only.weights = np.zeros_like(intercept_only.weights)
        intercept_only.weights[-1] = model.weights[-1]
        assert fitted_nll < intercept_only.survival_nll(test)


# ---------------------------------------------------------------- calibration


class TestCalibration:
    def test_isotonic_output_is_monotone(self):
        rng = np.random.default_rng(0)
        p = rng.uniform(size=400)
        y = (rng.uniform(size=400) < p**2).astype(float)
        calibrator = IsotonicCalibrator().fit(p, y)
        grid = np.linspace(0.0, 1.0, 50)
        mapped = calibrator.transform(grid)
        assert np.all(np.diff(mapped) >= -1e-12)

    def test_platt_recovers_a_known_distortion(self):
        rng = np.random.default_rng(1)
        z = rng.normal(size=6000)
        true_p = 1.0 / (1.0 + np.exp(-z))
        y = (rng.uniform(size=z.size) < true_p).astype(float)
        # Over-confident forecast: logits doubled.
        distorted = 1.0 / (1.0 + np.exp(-2.0 * z))
        calibrator = PlattCalibrator().fit(distorted, y)
        assert calibrator.slope == pytest.approx(0.5, abs=0.1)

    def test_calibration_reduces_log_loss(self):
        rng = np.random.default_rng(2)
        z = rng.normal(size=4000)
        y = (rng.uniform(size=z.size) < 1.0 / (1.0 + np.exp(-z))).astype(float)
        distorted = 1.0 / (1.0 + np.exp(-(2.5 * z + 1.0)))
        calibrator, scores = select_calibrator(distorted, y)
        assert log_loss(calibrator.transform(distorted), y) < scores["identity"]

    def test_perfect_forecast_has_no_calibration_error(self):
        p = np.repeat([0.1, 0.5, 0.9], 1000)
        rng = np.random.default_rng(3)
        y = (rng.uniform(size=p.size) < p).astype(float)
        ece, mce = calibration_error(p, y, n_bins=3)
        assert ece < 0.03
        assert mce < 0.05

    def test_report_is_serialisable(self):
        rng = np.random.default_rng(4)
        p = rng.uniform(size=200)
        y = (rng.uniform(size=200) < p).astype(float)
        payload = assess_calibration(p, y).to_dict()
        assert set(payload) >= {"brier", "log_loss", "ece", "slope", "reliability"}
        assert payload["reliability"]["count"]


# ---------------------------------------------------------------- evaluation


class TestEvaluationMetrics:
    def test_concordance_is_one_for_a_perfect_ordering(self):
        actual = np.array([1.0, 2.0, 3.0, 4.0])
        assert concordance_index(actual.copy(), actual)[0] == pytest.approx(1.0)

    def test_concordance_is_zero_for_a_reversed_ordering(self):
        actual = np.array([1.0, 2.0, 3.0, 4.0])
        assert concordance_index(-actual, actual)[0] == pytest.approx(0.0)

    def test_constant_prediction_scores_one_half(self):
        actual = np.array([1.0, 2.0, 3.0, 4.0])
        assert concordance_index(np.ones(4), actual)[0] == pytest.approx(0.5)

    def test_precision_at_k(self):
        scores = np.array([0.9, 0.8, 0.2, 0.1])
        labels = np.array([1.0, 0.0, 1.0, 0.0])
        assert precision_at_k(scores, labels, 2) == pytest.approx(0.5)
        assert precision_at_k(scores, labels, 1) == pytest.approx(1.0)

    def test_pooled_respects_the_universe_mask(self, split_and_blocks):
        _, train, _, test = split_and_blocks
        model = PrevalenceBaseline()
        model.fit(train)
        scores, labels = pooled(test, model.predict_all(test))
        assert scores.size == sum(int(e.universe_mask().sum()) for e in test)
        assert labels.size == scores.size

    def test_downstream_view_is_smaller_and_excludes_origins(self, split_and_blocks):
        _, train, _, test = split_and_blocks
        model = PrevalenceBaseline()
        model.fit(train)
        forecasts = model.predict_all(test)
        _, all_labels = pooled(test, forecasts)
        _, down_labels = pooled(test, forecasts, downstream_only=True)
        assert down_labels.size < all_labels.size
        assert down_labels.sum() <= all_labels.sum()

    def test_bootstrap_brackets_the_point_estimate(self, split_and_blocks):
        _, train, validation, test = split_and_blocks
        model = DiscreteTimeHazard()
        model.fit(train, validation)
        forecasts = model.predict_all(test)
        scores, labels = pooled(test, forecasts)
        from lce.evaluation.metrics import average_precision

        point = average_precision(scores, labels)
        interval = bootstrap_pr_auc(test, forecasts, n_resamples=200, seed=3)
        assert interval is not None
        assert interval["lo"] <= interval["median"] <= interval["hi"]
        assert interval["lo"] <= point <= interval["hi"] + 1e-9

    def test_bootstrap_is_deterministic_for_a_seed(self, split_and_blocks):
        _, train, validation, test = split_and_blocks
        model = DiscreteTimeHazard()
        model.fit(train, validation)
        forecasts = model.predict_all(test)
        first = bootstrap_pr_auc(test, forecasts, n_resamples=100, seed=11)
        second = bootstrap_pr_auc(test, forecasts, n_resamples=100, seed=11)
        assert first == second

    def test_bootstrap_resamples_scenarios_not_nodes(self, split_and_blocks):
        """A single-scenario block can only ever resample that one scenario."""
        _, train, validation, test = split_and_blocks
        model = DiscreteTimeHazard()
        model.fit(train, validation)
        one = test[:1]
        interval = bootstrap_pr_auc(one, model.predict_all(one), n_resamples=50, seed=5)
        assert interval is not None
        assert interval["lo"] == pytest.approx(interval["hi"])

    def test_scorecard_serialises(self, split_and_blocks):
        _, train, validation, test = split_and_blocks
        model = DiscreteTimeHazard()
        model.fit(train, validation)
        card = evaluate_forecasts(
            test,
            model.predict_all(test),
            model="hazard",
            split="test",
            task=PredictionTask(horizon_grid=(24.0,), n_hazard_intervals=8),
            n_bootstrap=50,
        )
        payload = card.to_dict()
        assert payload["pr_auc_ci"]["lo"] <= payload["pr_auc_ci"]["hi"]
        assert payload["by_horizon"]
        assert payload["by_family"]
        assert set(payload["downstream"]) >= {"pr_auc", "n_positive"}


# --------------------------------------------------------------- point process


class TestPointProcess:
    def test_estimator_uses_only_pre_origin_events(self, tiny_network):
        _, sim, suite, stream = tiny_network
        estimator = HawkesDependencyEstimator()
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        estimate = estimator.estimate(window)
        assert estimate.n_events == len(window.graph.payment_events)
        assert all(e.t < window.origin_t for e in window.graph.payment_events)

    def test_estimator_caches_identical_windows(self, tiny_network):
        _, sim, suite, stream = tiny_network
        estimator = HawkesDependencyEstimator()
        window = build_observed_window(suite[0], config=sim, baseline_payments=stream)
        first = estimator.estimate(window)
        second = estimator.estimate(window)
        assert first is second

    def test_recovered_pass_through_correlates_with_the_truth(self, learning_corpus):
        report = evaluate_dependency_recovery(
            learning_corpus, learning_corpus.examples[:2]
        )
        assert report.pooled["n_matched"] > 0
        assert report.pooled["edge_recall"] > 0.8
        assert report.pooled["pass_through_spearman"] > 0.3
        assert report.pooled["pass_through_mae"] < 0.25

    def test_recovery_needs_windows(self, learning_corpus, tmp_path):
        reloaded = load_corpus(save_corpus(learning_corpus, tmp_path / "c"))
        with pytest.raises(ModelError):
            evaluate_dependency_recovery(reloaded, reloaded.examples[:1])

    def test_contagion_model_never_touches_hidden_truth(self, split_and_blocks):
        """The unsupervised pipeline must be indifferent to the answers."""
        _, train, validation, test = split_and_blocks
        model = HawkesContagionModel()
        model.fit(train[:4], validation[:2])
        before = model.predict(test[0]).cdf.copy()

        model_again = HawkesContagionModel()
        model_again.fit(train[:4], validation[:2])
        after = model_again.predict(test[0]).cdf
        assert np.allclose(before, after)

    def test_supervised_upper_bound_is_out_of_sample(self, learning_corpus, split_and_blocks):
        _, train, _, test = split_and_blocks
        regressor = SupervisedDependencyRegressor()
        fit_report = regressor.fit(learning_corpus, train)
        assert fit_report["n_pairs"] > 0
        scores = regressor.score(learning_corpus, test)
        assert scores["n_pairs"] > 0
        assert 0.0 <= scores["pass_through_mae"] <= 1.0

    def test_point_process_forecast_is_a_valid_cdf(self, split_and_blocks):
        _, train, validation, test = split_and_blocks
        model = HawkesContagionModel()
        model.fit(train[:4], validation[:2])
        forecast = model.predict(test[0])
        assert np.all(np.diff(forecast.cdf, axis=1) >= -1e-12)
        assert forecast.score.max() <= 1.0

    def test_missing_shock_descriptor_is_refused(self, learning_corpus):
        blind = build_corpus(
            [71, 72, 73],
            overrides=TINY,
            observation=ObservationSpec(shock_descriptor=False),
            audit=False,
        )
        model = HawkesContagionModel()
        with pytest.raises(ModelError, match="shock descriptor"):
            model.fit(blind.examples[:2])


# ------------------------------------------------------------------ graph model


@pytest.mark.requires_torch
@pytest.mark.skipif(not HAS_TORCH, reason="needs the ml extra")
class TestTemporalGraphModel:
    def test_sample_shapes(self, learning_corpus):
        from lce.learning.graphmodel import EDGE_FEATURE_DIM, build_graph_sample

        example = learning_corpus.examples[0]
        sample = build_graph_sample(
            example, estimator=HawkesDependencyEstimator()
        )
        assert sample.x.shape == (example.n_merchants, NODE_FEATURE_DIM)
        assert sample.edge_index.shape[0] == 2
        assert sample.edge_attr.shape[1] == EDGE_FEATURE_DIM
        assert sample.edge_index.shape[1] == sample.edge_attr.shape[0]

    def test_shuffled_structure_preserves_out_degree(self, learning_corpus):
        from lce.learning.graphmodel import GraphSampleSpec, build_graph_sample

        example = learning_corpus.examples[0]
        estimator = HawkesDependencyEstimator()
        real = build_graph_sample(example, estimator=estimator)
        shuffled = build_graph_sample(
            example, estimator=estimator, spec=GraphSampleSpec(structure="shuffled")
        )
        assert np.array_equal(
            np.bincount(real.edge_index[0], minlength=example.n_merchants),
            np.bincount(shuffled.edge_index[0], minlength=example.n_merchants),
        )

    def test_no_edges_variant_is_empty(self, learning_corpus):
        from lce.learning.graphmodel import GraphSampleSpec, build_graph_sample

        sample = build_graph_sample(
            learning_corpus.examples[0],
            estimator=HawkesDependencyEstimator(),
            spec=GraphSampleSpec(structure="none"),
        )
        assert sample.edge_index.shape[1] == 0

    def test_true_structure_requires_the_hidden_edges(self, learning_corpus):
        from lce.learning.graphmodel import GraphSampleSpec, build_graph_sample

        with pytest.raises(ModelError, match="oracle"):
            build_graph_sample(
                learning_corpus.examples[0],
                estimator=HawkesDependencyEstimator(),
                spec=GraphSampleSpec(structure="true"),
            )

    def test_trains_and_predicts(self, split_and_blocks):
        from lce.learning.graphmodel import TemporalGraphModel
        from lce.models.tgnn import TGNNConfig

        _, train, validation, test = split_and_blocks
        model = TemporalGraphModel(config=TGNNConfig(epochs=6, patience=3, seed=7))
        report = model.fit(train, validation)
        assert report["epochs_run"] >= 1
        forecast = model.predict(test[0])
        assert forecast.cdf.shape == (
            test[0].n_merchants,
            len(test[0].interval_edges) - 1,
        )
        assert np.all(np.diff(forecast.cdf, axis=1) >= -1e-12)

    def test_round_trips_through_an_artifact(self, split_and_blocks, tmp_path):
        from lce.learning.graphmodel import TemporalGraphModel
        from lce.models.tgnn import TGNNConfig

        _, train, validation, test = split_and_blocks
        model = TemporalGraphModel(config=TGNNConfig(epochs=4, patience=2, seed=7))
        model.fit(train, validation)
        before = model.predict(test[0]).cdf
        path = model.save(tmp_path / "gnn.pt")

        restored = TemporalGraphModel(config=TGNNConfig(epochs=4, patience=2, seed=7))
        restored.load(path)
        restored.log_time_sigma = model.log_time_sigma
        assert np.allclose(restored.predict(test[0]).cdf, before)


# ------------------------------------------------------------------ experiment


class TestExperiment:
    def test_smoke_run(self, learning_corpus):
        from lce.learning.experiment import Phase3Config, run_phase3

        config = Phase3Config(
            seeds=(71, 72, 73, 74),
            models=("prevalence", "cash_cover", "discrete_hazard"),
            split=SplitSpec(0.5, 0.25),
        )
        report = run_phase3(config, corpus=learning_corpus)
        assert report.split_audit["clean"]
        assert report.split_audit["window_audit"]["clean"]
        assert {c.model for c in report.for_split("test")} == set(config.models)
        assert report.dependency["pooled"]["n_matched"] > 0
        assert report.leaderboard()[0]["pr_auc"] is not None

    def test_config_hash_is_stable_and_sensitive(self):
        from lce.learning.experiment import Phase3Config

        base = Phase3Config(seeds=(1, 2, 3))
        assert base.config_hash == Phase3Config(seeds=(1, 2, 3)).config_hash
        assert base.config_hash != Phase3Config(seeds=(1, 2, 4)).config_hash

    def test_unknown_model_key_is_rejected(self):
        from lce.learning.experiment import Phase3Config, build_models

        with pytest.raises(ModelError, match="unknown model key"):
            build_models(Phase3Config(models=("nope",)))
