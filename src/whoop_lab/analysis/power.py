"""Minimum detectable effect for a planned design, from baseline variability.

Simulates the actual design (permuted blocks, planned days, expected missing
mornings, autocorrelated noise) and the actual test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from whoop_lab.analysis.inference import FloatArray, RandomizationTest
from whoop_lab.analysis.simulate import simulate_experiment

MIN_BASELINE_VALUES = 10


@dataclass(frozen=True)
class Baseline:
    n_values: int
    n_days: int
    sigma: float
    phi: float
    missing_rate: float


def summarize_baseline(values: FloatArray) -> Baseline:
    """`values`: the outcome on consecutive days, NaN where missing."""
    present = values[~np.isnan(values)]
    if len(present) < MIN_BASELINE_VALUES:
        raise ValueError(
            f"need at least {MIN_BASELINE_VALUES} baseline mornings, have {len(present)}"
        )
    pairs = ~np.isnan(values[:-1]) & ~np.isnan(values[1:])
    phi = 0.0
    if pairs.sum() >= 5:
        today, tomorrow = values[:-1][pairs], values[1:][pairs]
        if today.std() > 0 and tomorrow.std() > 0:
            phi = float(np.corrcoef(today, tomorrow)[0, 1])
    return Baseline(
        n_values=len(present),
        n_days=len(values),
        sigma=float(present.std(ddof=1)),
        phi=float(np.clip(phi, 0.0, 0.9)),
        missing_rate=float(np.isnan(values).mean()),
    )


@dataclass(frozen=True)
class PowerResult:
    days: int
    block_size: int
    power: float
    alpha: float
    minimum_detectable_effect: float | None
    sims: int


def minimum_detectable_effect(
    baseline: Baseline,
    *,
    days: int,
    block_size: int = 4,
    power: float = 0.80,
    alpha: float = 0.05,
    sims: int = 500,
    draws: int = 1000,
    seed: int = 0,
    missing_rate: float | None = None,
) -> PowerResult:
    """Smallest constant additive effect the randomization test detects with the given power.

    The same simulated experiments are reused for every candidate effect, so the
    power curve is monotone and bisection is stable.
    """
    rng = np.random.default_rng(seed)
    rate = baseline.missing_rate if missing_rate is None else missing_rate
    tests = []
    for i in range(sims):
        sim = simulate_experiment(
            rng,
            n_days=days,
            block_size=block_size,
            effect=0.0,
            phi=baseline.phi,
            sigma=baseline.sigma,
            missing_rate=rate,
        )
        tests.append(RandomizationTest(sim.y, sim.arm, sim.design, draws=draws, seed=i))

    def achieved(effect: float) -> float:
        # Adding a constant effect e to the treated days is the same as testing
        # H0: effect == -e on the noise alone.
        rejected = 0
        for test in tests:
            p = test.p_value(-effect)
            rejected += int(p is not None and p <= alpha)
        return rejected / sims

    low, high = 0.0, baseline.sigma
    for _ in range(12):
        if achieved(high) >= power:
            break
        low, high = high, high * 2
    else:
        return PowerResult(days, block_size, power, alpha, None, sims)
    for _ in range(30):
        middle = (low + high) / 2
        if achieved(middle) >= power:
            high = middle
        else:
            low = middle
    return PowerResult(days, block_size, power, alpha, high, sims)
