"""Simulated experiments, used to validate the analysis before it sees real data.

An analysis pipeline that has only ever seen real data cannot be told apart
from one with a bug. `validate()` runs the checks the experiment-analysis skill
requires before any analysis change is committed.

Days are assigned here with the assigner's own rules (`randomize.next_slot`,
`in_washout`, `stratum_for`, `arm_for`), not with the test's re-draw code, so
the test is checked against the design and not against itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from whoop_lab.analysis.inference import BlockDesign, FloatArray, RandomizationTest
from whoop_lab.experiments.randomize import Slot, arm_for, in_washout, next_slot, stratum_for
from whoop_lab.models.experiments import ARM_TREATMENT

_FIRST_DAY = date(2026, 1, 5)  # a Monday


def ar1_noise(n: int, phi: float, sigma: float, rng: np.random.Generator) -> FloatArray:
    """Stationary AR(1) noise with marginal standard deviation `sigma`."""
    innovations = rng.normal(0.0, sigma * np.sqrt(1.0 - phi**2), size=n)
    noise = np.empty(n)
    noise[0] = rng.normal(0.0, sigma)
    for t in range(1, n):
        noise[t] = phi * noise[t - 1] + innovations[t]
    return noise


@dataclass(frozen=True)
class Scenario:
    """A design and the conditions it runs under."""

    name: str = "default"
    planned_days: int = 28
    block_size: int = 4
    washout_days: int = 0
    stratify_weekend: bool = False
    skip_rate: float = 0.0  # chance that an evening is missed and its day left unassigned
    trend_per_day: float = 0.0  # linear drift of the outcome, in sigmas per calendar day
    analysed_days: int | None = None  # analyse only the first so many assigned days


@dataclass(frozen=True)
class SimulatedExperiment:
    y: FloatArray
    arm: FloatArray
    design: BlockDesign


def simulate_scenario(
    rng: np.random.Generator,
    scenario: Scenario,
    *,
    effect: float = 0.0,
    phi: float = 0.4,
    sigma: float = 0.15,
    missing_rate: float = 0.0,
) -> SimulatedExperiment:
    """One experiment, assigned day by day as the service would assign it.

    Noise is AR(1) over calendar days, so washout and missed days weaken the
    correlation between neighbouring assigned days exactly as they would in life.
    Outcomes are missing completely at random.
    """
    start = _FIRST_DAY + timedelta(days=int(rng.integers(7)) if scenario.stratify_weekend else 0)
    offsets: list[int] = []
    strata: list[str] = []
    slots: list[Slot] = []
    last_slot: dict[str, Slot] = {}
    latest: date | None = None
    offset = -1
    while len(offsets) < scenario.planned_days:
        offset += 1
        day = start + timedelta(days=offset)
        if in_washout(day, latest, scenario.washout_days):
            continue
        if scenario.skip_rate > 0 and rng.random() < scenario.skip_rate:
            continue
        stratum = stratum_for(day, stratify_weekend=scenario.stratify_weekend)
        slot = next_slot(
            last_slot.get(stratum), scenario.block_size, lambda: int(rng.integers(2**62))
        )
        last_slot[stratum] = slot
        latest = day
        offsets.append(offset)
        strata.append(stratum)
        slots.append(slot)

    arm = np.array(
        [
            float(arm_for(slot.seed, scenario.block_size, slot.block_position) == ARM_TREATMENT)
            for slot in slots
        ]
    )
    calendar = np.array(offsets)
    noise = ar1_noise(offset + 1, phi, sigma, rng)[calendar]
    y = noise + scenario.trend_per_day * sigma * calendar + effect * arm
    if missing_rate > 0:
        y[rng.random(len(y)) < missing_rate] = np.nan

    keep = scenario.analysed_days or scenario.planned_days
    design = BlockDesign.from_assignments(
        strata[:keep],
        [slot.block_index for slot in slots[:keep]],
        [slot.block_position for slot in slots[:keep]],
        scenario.block_size,
    )
    return SimulatedExperiment(y[:keep], arm[:keep], design)


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
    """The plain design: consecutive days, permuted blocks, no strata or washout."""
    return simulate_scenario(
        rng,
        Scenario(planned_days=n_days, block_size=block_size),
        effect=effect,
        phi=phi,
        sigma=sigma,
        missing_rate=missing_rate,
    )


def plain_shuffle(n_days: int) -> BlockDesign:
    """The WRONG reference distribution: arms shuffled over all days, ignoring blocks."""
    return BlockDesign.contiguous(n_days, n_days + n_days % 2)


# The harder designs every analysis change is checked against, as well as the default.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(name="weekday/weekend strata", stratify_weekend=True),
    Scenario(name="washout of 2 days under a drift", washout_days=2, trend_per_day=0.05),
    Scenario(name="missed evenings", skip_rate=0.1),
    Scenario(name="analysed mid-experiment", analysed_days=18),
)

REJECTION_BAND = (0.03, 0.07)
COVERAGE_BAND = (0.93, 0.97)
# With autocorrelated noise a plain shuffle is too cautious. On the same simulated
# experiments it must reject less often than the design-based test by at least this.
MIN_SHUFFLE_GAP = 0.01


def _within(value: float, band: tuple[float, float]) -> bool:
    return band[0] <= value <= band[1]


@dataclass(frozen=True)
class Calibration:
    """Null rejection and planted-effect recovery for one scenario."""

    name: str
    n_sims: int
    null_rejection_rate: float
    planted_effect: float
    mean_estimate: float
    estimate_standard_error: float
    coverage: float

    @property
    def null_calibrated(self) -> bool:
        return _within(self.null_rejection_rate, REJECTION_BAND)

    @property
    def unbiased(self) -> bool:
        """Within 10% of the planted effect, and within simulation error of it."""
        error = abs(self.mean_estimate - self.planted_effect)
        return (
            error <= 0.10 * abs(self.planted_effect) and error <= 3 * self.estimate_standard_error
        )

    @property
    def effect_recovered(self) -> bool:
        return self.unbiased and _within(self.coverage, COVERAGE_BAND)

    @property
    def passed(self) -> bool:
        return self.null_calibrated and self.effect_recovered


@dataclass(frozen=True)
class ValidationReport:
    default: Calibration
    shuffle_rejection_rate: float
    mismatch_rejection_rate: float
    scenarios: tuple[Calibration, ...]

    @property
    def n_sims(self) -> int:
        return self.default.n_sims

    @property
    def null_rejection_rate(self) -> float:
        return self.default.null_rejection_rate

    @property
    def planted_effect(self) -> float:
        return self.default.planted_effect

    @property
    def mean_estimate(self) -> float:
        return self.default.mean_estimate

    @property
    def coverage(self) -> float:
        return self.default.coverage

    @property
    def null_calibrated(self) -> bool:
        return self.default.null_calibrated

    @property
    def effect_recovered(self) -> bool:
        return self.default.effect_recovered

    @property
    def design_matched(self) -> bool:
        """The test must FAIL calibration when it assumes a design the data did not use."""
        return not _within(self.mismatch_rejection_rate, REJECTION_BAND)

    @property
    def shuffle_told_apart(self) -> bool:
        """A plain shuffle of labels must be distinguishable from the real test."""
        return self.null_rejection_rate - self.shuffle_rejection_rate >= MIN_SHUFFLE_GAP

    @property
    def scenarios_passed(self) -> bool:
        return all(scenario.passed for scenario in self.scenarios)

    @property
    def passed(self) -> bool:
        return (
            self.null_calibrated
            and self.effect_recovered
            and self.design_matched
            and self.shuffle_told_apart
            and self.scenarios_passed
        )


def _calibrate(
    rng: np.random.Generator,
    scenario: Scenario,
    *,
    n_sims: int,
    phi: float,
    sigma: float,
    effect: float,
    missing_rate: float,
    draws: int,
    alpha: float,
    with_shuffle: bool = False,
) -> tuple[Calibration, float]:
    """Returns the calibration and, if asked, the plain-shuffle rejection rate on
    the same null experiments."""
    rejections = shuffled = 0
    for i in range(n_sims):
        sim = simulate_scenario(rng, scenario, phi=phi, sigma=sigma, missing_rate=missing_rate)
        p = RandomizationTest(sim.y, sim.arm, sim.design, draws=draws, seed=i).p_value()
        rejections += int(p is not None and p <= alpha)
        if with_shuffle:
            wrong = plain_shuffle(len(sim.y))
            p = RandomizationTest(sim.y, sim.arm, wrong, draws=draws, seed=i).p_value()
            shuffled += int(p is not None and p <= alpha)

    estimates: list[float] = []
    covered = 0
    for i in range(n_sims):
        sim = simulate_scenario(
            rng, scenario, effect=effect, phi=phi, sigma=sigma, missing_rate=missing_rate
        )
        test = RandomizationTest(sim.y, sim.arm, sim.design, draws=draws, seed=i)
        low, high = test.confidence_interval(alpha)
        if test.observed is not None:
            estimates.append(test.observed)
        covered += int(low is not None and high is not None and low <= effect <= high)

    calibration = Calibration(
        name=scenario.name,
        n_sims=n_sims,
        null_rejection_rate=rejections / n_sims,
        planted_effect=effect,
        mean_estimate=float(np.mean(estimates)),
        estimate_standard_error=float(np.std(estimates, ddof=1) / np.sqrt(len(estimates))),
        coverage=covered / n_sims,
    )
    return calibration, shuffled / n_sims


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
    scenarios: tuple[Scenario, ...] = SCENARIOS,
) -> ValidationReport:
    rng = np.random.default_rng(seed)

    def run(scenario: Scenario, *, with_shuffle: bool = False) -> tuple[Calibration, float]:
        return _calibrate(
            rng,
            scenario,
            n_sims=n_sims,
            phi=phi,
            sigma=sigma,
            effect=effect,
            missing_rate=missing_rate,
            draws=draws,
            alpha=alpha,
            with_shuffle=with_shuffle,
        )

    default, shuffle_rate = run(
        Scenario(planned_days=n_days, block_size=block_size), with_shuffle=True
    )

    # Data assigned with one block size, analysed as if it were another.
    mismatch = mismatch_block_size if mismatch_block_size is not None else n_days + n_days % 2
    mismatched = 0
    for i in range(n_sims):
        sim = simulate_scenario(
            rng, Scenario(planned_days=n_days, block_size=mismatch), phi=phi, sigma=sigma
        )
        wrong_design = BlockDesign.contiguous(n_days, block_size)
        p = RandomizationTest(sim.y, sim.arm, wrong_design, draws=draws, seed=i).p_value()
        mismatched += int(p is not None and p <= alpha)

    return ValidationReport(
        default=default,
        shuffle_rejection_rate=shuffle_rate,
        mismatch_rejection_rate=mismatched / n_sims,
        scenarios=tuple(run(scenario)[0] for scenario in scenarios),
    )
