"""Simulated experiments, used to validate the analysis before it sees real data.

An analysis pipeline that has only ever seen real data cannot be told apart
from one with a bug. `validate()` runs the three checks the experiment-analysis
skill requires before any analysis change is committed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from whoop_lab.analysis.inference import BlockDesign, FloatArray, RandomizationTest


def ar1_noise(n: int, phi: float, sigma: float, rng: np.random.Generator) -> FloatArray:
    """Stationary AR(1) noise with marginal standard deviation `sigma`."""
    innovations = rng.normal(0.0, sigma * np.sqrt(1.0 - phi**2), size=n)
    noise = np.empty(n)
    noise[0] = rng.normal(0.0, sigma)
    for t in range(1, n):
        noise[t] = phi * noise[t - 1] + innovations[t]
    return noise


@dataclass(frozen=True)
class SimulatedExperiment:
    y: FloatArray
    arm: FloatArray
    design: BlockDesign


def simulate_experiment(
    rng: np.random.Generator,
    *,
    n_days: int = 28,
    block_size: int = 4,
    effect: float = 0.0,
    phi: float = 0.4,
    sigma: float = 0.15,
    missing_rate: float = 0.0,
) -> SimulatedExperiment:
    """One experiment: permuted-block assignment, AR(1) noise, a constant additive
    effect, and outcomes missing completely at random."""
    design = BlockDesign.contiguous(n_days, block_size)
    arm = design.draw(rng, 1)[0].astype(np.float64)
    y = ar1_noise(n_days, phi, sigma, rng) + effect * arm
    if missing_rate > 0:
        y[rng.random(n_days) < missing_rate] = np.nan
    return SimulatedExperiment(y, arm, design)


@dataclass(frozen=True)
class ValidationReport:
    n_sims: int
    null_rejection_rate: float
    planted_effect: float
    mean_estimate: float
    coverage: float
    mismatch_rejection_rate: float

    @property
    def null_calibrated(self) -> bool:
        return 0.03 <= self.null_rejection_rate <= 0.07

    @property
    def effect_recovered(self) -> bool:
        close = abs(self.mean_estimate - self.planted_effect) <= 0.10 * abs(self.planted_effect)
        return close and 0.93 <= self.coverage <= 0.97

    @property
    def design_matched(self) -> bool:
        """The test must FAIL calibration when it assumes a design the data did not use."""
        return not 0.03 <= self.mismatch_rejection_rate <= 0.07

    @property
    def passed(self) -> bool:
        return self.null_calibrated and self.effect_recovered and self.design_matched


def validate(
    *,
    n_sims: int = 2000,
    n_days: int = 28,
    block_size: int = 4,
    phi: float = 0.4,
    sigma: float = 0.15,
    effect: float = 0.15,
    missing_rate: float = 0.1,
    draws: int = 2000,
    alpha: float = 0.05,
    seed: int = 20261111,
    mismatch_block_size: int | None = None,
) -> ValidationReport:
    rng = np.random.default_rng(seed)
    mismatch = mismatch_block_size if mismatch_block_size is not None else n_days

    rejections = 0
    for i in range(n_sims):
        sim = simulate_experiment(
            rng,
            n_days=n_days,
            block_size=block_size,
            phi=phi,
            sigma=sigma,
            missing_rate=missing_rate,
        )
        p = RandomizationTest(sim.y, sim.arm, sim.design, draws=draws, seed=i).p_value()
        rejections += int(p is not None and p <= alpha)

    estimates: list[float] = []
    covered = 0
    for i in range(n_sims):
        sim = simulate_experiment(
            rng,
            n_days=n_days,
            block_size=block_size,
            effect=effect,
            phi=phi,
            sigma=sigma,
            missing_rate=missing_rate,
        )
        test = RandomizationTest(sim.y, sim.arm, sim.design, draws=draws, seed=i)
        low, high = test.confidence_interval(alpha)
        if test.observed is not None:
            estimates.append(test.observed)
        covered += int(low is not None and high is not None and low <= effect <= high)

    # Data assigned with one block size, analysed as if it were another.
    mismatched = 0
    for i in range(n_sims):
        sim = simulate_experiment(
            rng, n_days=n_days, block_size=mismatch, phi=phi, sigma=sigma, missing_rate=0.0
        )
        wrong_design = BlockDesign.contiguous(n_days, block_size)
        p = RandomizationTest(sim.y, sim.arm, wrong_design, draws=draws, seed=i).p_value()
        mismatched += int(p is not None and p <= alpha)

    return ValidationReport(
        n_sims=n_sims,
        null_rejection_rate=rejections / n_sims,
        planted_effect=effect,
        mean_estimate=float(np.mean(estimates)),
        coverage=covered / n_sims,
        mismatch_rejection_rate=mismatched / n_sims,
    )
