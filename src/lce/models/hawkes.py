"""Marked Hawkes EM kernel for one directed edge.

Why the amounts have to be part of the model
--------------------------------------------
A time-only Hawkes kernel attributes each outgoing payment to whichever inflow
happened most recently. On a real network that is wrong in a specific and
damaging way: a merchant receives a steady drizzle of small payments alongside
occasional large ones, and the time-only kernel hands responsibility for a large
outflow to whatever small inflow happened to land just before it. The estimated
amount ratio :math:`a_o / a_m` then explodes, and pass-through saturates at 1.

So the process is treated as **marked**: a triggered payment carries an amount
drawn around :math:`\\theta a_m`,

.. math::

    \\log(a_o / a_m) \\sim \\mathcal{N}(\\log\\theta,\\ \\sigma_r^2)

and the parent responsibilities weigh time proximity *and* amount plausibility:

.. math::

    w_m(o) \\;\\propto\\; \\underbrace{\\alpha\\beta e^{-\\beta(t_o - t_m)}}_{\\text{timing}}
                    \\cdot \\underbrace{f_{\\mathcal{N}}\\!\\big(\\log(a_o/a_m);
                       \\log\\theta, \\sigma_r\\big)}_{\\text{amount plausibility}}

The background/excitation split stays timing-driven, because the baseline stream
has no amount relationship to any inflow and giving it a mark density would mean
inventing one. Within the excitation mass, though, amount compatibility decides
the parent - which is what makes :math:`\\theta` identifiable.

:math:`\\theta` and :math:`\\sigma_r` are then re-estimated in the M-step as the
responsibility-weighted **geometric** mean and spread of the ratios, which is
the maximum-likelihood estimator for a multiplicative model. An arithmetic mean
would be biased upward by the heavy right tail.

Everything is vectorised over (child, parent) pairs: the flattened pair index is
built once per kernel setting, so each EM iteration is a handful of NumPy
reductions rather than a Python loop over events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)


@dataclass(slots=True)
class MarkedHawkesFit:
    """Converged parameters for one edge."""

    mu: float
    alpha: float
    beta: float
    theta: float
    sigma_ratio: float
    conditional_probability: float
    lag_mu_log: float
    lag_sigma_log: float
    lag_weight: float
    excitation_mass: float
    background_mass: float
    log_likelihood: float
    iterations: int
    converged: bool


def _pair_index(
    lo: np.ndarray, hi: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Flatten per-child parent ranges into (child_idx, parent_idx, counts).

    ``lo``/``hi`` are the half-open parent windows per child. The flattening
    trick avoids a Python loop: positions within each child's block are
    recovered by subtracting the block's start offset from a global arange.
    """
    counts = np.maximum(hi - lo, 0).astype(np.int64)
    total = int(counts.sum())
    if total == 0:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty, counts

    child_idx = np.repeat(np.arange(counts.size, dtype=np.int64), counts)
    block_start = np.concatenate(([0], np.cumsum(counts)[:-1]))
    within = np.arange(total, dtype=np.int64) - np.repeat(block_start, counts)
    parent_idx = np.repeat(lo.astype(np.int64), counts) + within
    return child_idx, parent_idx, counts


def fit_marked_hawkes(
    outflow_times: np.ndarray,
    outflow_amounts: np.ndarray,
    inflow_times: np.ndarray,
    inflow_amounts: np.ndarray,
    *,
    window: float,
    beta: float,
    theta_init: float = 0.4,
    sigma_init: float = 0.8,
    alpha_init: float = 0.5,
    alpha_max: float = 5.0,
    max_parents: int = 64,
    kernel_window_multiple: float = 8.0,
    iterations: int = 40,
    tolerance: float = 1e-6,
    refit_beta: bool = True,
    sigma_floor: float = 0.15,
    sigma_ceiling: float = 2.5,
) -> MarkedHawkesFit:
    """Fit the marked Hawkes model for one edge by EM."""
    n_out = int(outflow_times.size)
    n_in = int(inflow_times.size)
    if n_out == 0 or n_in == 0:
        return MarkedHawkesFit(
            mu=n_out / max(window, 1.0),
            alpha=0.0,
            beta=beta,
            theta=0.0,
            sigma_ratio=sigma_init,
            conditional_probability=0.0,
            lag_mu_log=math.log(max(1.0 / max(beta, 1e-9), 1e-6)),
            lag_sigma_log=1.0,
            lag_weight=0.0,
            excitation_mass=0.0,
            background_mass=float(n_out),
            log_likelihood=float("nan"),
            iterations=0,
            converged=False,
        )

    mu = max(n_out / max(window, 1.0), 1e-12)
    alpha = alpha_init
    theta = max(theta_init, 1e-4)
    sigma = float(np.clip(sigma_init, sigma_floor, sigma_ceiling))

    child_idx = parent_idx = np.empty(0, dtype=np.int64)
    dt = log_ratio = np.empty(0, dtype=float)
    current_beta = -1.0
    log_likelihood = float("nan")
    converged = False
    iters = 0

    excit_total = 0.0
    bg_total = float(n_out)
    lag_mu_log, lag_sigma_log, lag_weight = math.log(1.0 / max(beta, 1e-9)), 1.0, 0.0

    for it in range(iterations):
        iters = it + 1

        # The parent window depends on beta, so the pair index is rebuilt only
        # when beta actually moves.
        if abs(current_beta - beta) > 1e-12:
            horizon = kernel_window_multiple / max(beta, 1e-9)
            hi = np.searchsorted(inflow_times, outflow_times, side="left")
            lo = np.searchsorted(inflow_times, outflow_times - horizon, side="left")
            lo = np.maximum(lo, hi - max_parents)
            child_idx, parent_idx, _ = _pair_index(lo, hi)
            if child_idx.size == 0:
                break
            dt = outflow_times[child_idx] - inflow_times[parent_idx]
            valid = dt > 0
            child_idx, parent_idx, dt = child_idx[valid], parent_idx[valid], dt[valid]
            if child_idx.size == 0:
                break
            log_ratio = np.log(
                np.maximum(outflow_amounts[child_idx], 1e-9)
                / np.maximum(inflow_amounts[parent_idx], 1e-9)
            )
            current_beta = beta

        # --- E-step ---------------------------------------------------------
        timing = alpha * beta * np.exp(-beta * dt)
        z = (log_ratio - math.log(theta)) / sigma
        amount_density = np.exp(-0.5 * z * z) / (sigma * math.exp(_LOG_SQRT_2PI))

        # Background/excitation split is timing-only (see module docstring);
        # amount plausibility only redistributes mass *within* the parents.
        timing_sum = np.bincount(child_idx, weights=timing, minlength=n_out)
        lam = mu + timing_sum
        p_excite_child = np.divide(
            timing_sum, lam, out=np.zeros_like(lam), where=lam > 0
        )

        weight = timing * amount_density
        weight_sum = np.bincount(child_idx, weights=weight, minlength=n_out)
        share = np.divide(
            weight, weight_sum[child_idx],
            out=np.zeros_like(weight),
            where=weight_sum[child_idx] > 0,
        )
        resp = share * p_excite_child[child_idx]

        # --- M-step ---------------------------------------------------------
        excit_total = float(resp.sum())
        bg_total = float(n_out - excit_total)
        new_mu = max(bg_total / max(window, 1.0), 1e-12)
        new_alpha = float(np.clip(excit_total / max(n_in, 1), 0.0, alpha_max))

        total_resp = excit_total
        if total_resp > 1e-9:
            mean_log_ratio = float(np.sum(resp * log_ratio) / total_resp)
            var_log_ratio = max(
                float(np.sum(resp * (log_ratio - mean_log_ratio) ** 2) / total_resp), 1e-6
            )
            # Geometric mean: the ML estimate under a log-normal mark model.
            new_theta = float(np.exp(mean_log_ratio))
            new_sigma = float(np.clip(math.sqrt(var_log_ratio), sigma_floor, sigma_ceiling))

            log_dt = np.log(dt)
            lag_weight = total_resp
            lag_mu_log = float(np.sum(resp * log_dt) / total_resp)
            lag_sigma_log = float(
                math.sqrt(
                    max(float(np.sum(resp * (log_dt - lag_mu_log) ** 2) / total_resp), 1e-6)
                )
            )
        else:
            new_theta, new_sigma = theta, sigma

        log_likelihood = float(np.sum(np.log(np.maximum(lam, 1e-300)))) - (
            new_mu * window + new_alpha * n_in
        )

        delta = (
            abs(new_mu - mu)
            + abs(new_alpha - alpha)
            + abs(new_theta - theta)
            + abs(new_sigma - sigma)
        )
        mu, alpha, theta, sigma = new_mu, new_alpha, new_theta, new_sigma

        if refit_beta and lag_weight > 1.0:
            # E[lag] under the refitted log-normal; beta is its reciprocal.
            beta = 1.0 / max(math.exp(lag_mu_log + 0.5 * lag_sigma_log**2), 1e-3)

        if delta < tolerance:
            converged = True
            break

    return MarkedHawkesFit(
        mu=mu,
        alpha=alpha,
        beta=beta,
        theta=theta,
        sigma_ratio=sigma,
        conditional_probability=min(1.0, alpha),
        lag_mu_log=lag_mu_log,
        lag_sigma_log=lag_sigma_log,
        lag_weight=lag_weight,
        excitation_mass=excit_total,
        background_mass=bg_total,
        log_likelihood=log_likelihood,
        iterations=iters,
        converged=converged,
    )
