"""The validation the experiment-analysis skill requires before any analysis change."""

from __future__ import annotations

import numpy as np
import pytest

from whoop_lab.analysis.inference import BlockDesign
from whoop_lab.analysis.simulate import (
    SCENARIOS,
    Calibration,
    Scenario,
    ValidationReport,
    ar1_noise,
    plain_shuffle,
    simulate_scenario,
    validate,
)


@pytest.fixture(scope="module")
def report() -> ValidationReport:
    return validate(n_sims=2000, phi=0.4)


def test_null_false_positive_rate_is_near_five_percent(report: ValidationReport) -> None:
    assert report.n_sims >= 2000
    assert 0.03 <= report.null_rejection_rate <= 0.07
    assert report.null_calibrated


def test_planted_effect_is_recovered(report: ValidationReport) -> None:
    assert report.mean_estimate == pytest.approx(report.planted_effect, rel=0.10)
    assert report.default.unbiased
    assert 0.93 <= report.coverage <= 0.97
    assert report.effect_recovered


def test_the_test_is_tied_to_the_design(report: ValidationReport) -> None:
    """Data assigned without the block structure, analysed as if it had it, must
    fail calibration. If this ever passes calibration, the test ignores the design."""
    assert report.mismatch_rejection_rate > 0.07
    assert report.design_matched


def test_a_plain_shuffle_of_labels_is_told_apart(report: ValidationReport) -> None:
    """The wrong test for this design is too cautious under autocorrelation. If the
    real test ever drifts to the same rejection rate, it has stopped using the blocks."""
    assert report.shuffle_rejection_rate < report.null_rejection_rate - 0.01
    assert report.shuffle_told_apart


def test_harder_designs_are_calibrated_through_the_assigner(report: ValidationReport) -> None:
    assert [s.name for s in report.scenarios] == [s.name for s in SCENARIOS]
    for scenario in report.scenarios:
        assert scenario.null_calibrated, scenario
        assert scenario.effect_recovered, scenario
    assert report.passed


def test_calibration_holds_with_stronger_autocorrelation() -> None:
    strong = validate(n_sims=2000, phi=0.5, seed=7, scenarios=())
    assert strong.null_calibrated
    assert strong.effect_recovered
    assert strong.shuffle_told_apart
    assert strong.passed


def test_a_biased_estimate_fails_even_inside_ten_percent() -> None:
    # 6% off with a small simulation error: the old "within 10%" rule alone would pass it.
    biased = Calibration("x", 2000, 0.05, 0.15, 0.141, 0.0013, 0.95)
    assert not biased.unbiased
    assert not biased.passed


def test_simulator_noise_is_autocorrelated_as_specified() -> None:
    rng = np.random.default_rng(0)
    noise = ar1_noise(200_000, 0.4, 0.15, rng)
    assert noise.std() == pytest.approx(0.15, rel=0.02)
    assert np.corrcoef(noise[:-1], noise[1:])[0, 1] == pytest.approx(0.4, abs=0.01)


def test_simulated_days_follow_the_assigner_rules() -> None:
    rng = np.random.default_rng(4)
    scenario = Scenario(planned_days=24, stratify_weekend=True, washout_days=1, skip_rate=0.1)
    sim = simulate_scenario(rng, scenario, effect=1.0, sigma=0.01)
    assert len(sim.y) == len(sim.arm) == sim.design.n_days == 24
    # Every complete block holds two treatment days.
    for block in range(sim.design.n_blocks):
        members = sim.design.block_of_day == block
        if members.sum() == 4:
            assert sim.arm[members].sum() == 2
    # The planted effect lands on exactly the treatment days.
    assert (sim.y[sim.arm == 1].min() - sim.y[sim.arm == 0].max()) > 0.5


def test_mid_experiment_scenario_cuts_through_a_block() -> None:
    sim = simulate_scenario(np.random.default_rng(0), Scenario(analysed_days=18))
    assert sim.design.n_days == 18
    assert (sim.design.block_of_day == 4).sum() == 2


def test_plain_shuffle_ignores_blocks() -> None:
    design = plain_shuffle(28)
    assert isinstance(design, BlockDesign)
    draws = design.draw(np.random.default_rng(0), 500)
    assert (draws.sum(axis=1) == 14).all()
    assert (draws.reshape(500, 7, 4).sum(axis=2) != 2).any()
