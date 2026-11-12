"""The validation the experiment-analysis skill requires before any analysis change."""

from __future__ import annotations

import numpy as np
import pytest

from whoop_lab.analysis.simulate import ValidationReport, ar1_noise, validate


@pytest.fixture(scope="module")
def report() -> ValidationReport:
    return validate(n_sims=2000, phi=0.4)


def test_null_false_positive_rate_is_near_five_percent(report: ValidationReport) -> None:
    assert report.n_sims >= 2000
    assert 0.03 <= report.null_rejection_rate <= 0.07
    assert report.null_calibrated


def test_planted_effect_is_recovered(report: ValidationReport) -> None:
    assert report.mean_estimate == pytest.approx(report.planted_effect, rel=0.10)
    assert 0.93 <= report.coverage <= 0.97
    assert report.effect_recovered


def test_the_test_is_tied_to_the_design(report: ValidationReport) -> None:
    """Data assigned without the block structure, analysed as if it had it, must
    fail calibration. If this ever passes calibration, the test ignores the design."""
    assert not 0.03 <= report.mismatch_rejection_rate <= 0.07
    assert report.design_matched
    assert report.passed


def test_calibration_holds_with_stronger_autocorrelation() -> None:
    strong = validate(n_sims=2000, phi=0.5, seed=7)
    assert strong.null_calibrated
    assert strong.effect_recovered


def test_simulator_noise_is_autocorrelated_as_specified() -> None:
    rng = np.random.default_rng(0)
    noise = ar1_noise(200_000, 0.4, 0.15, rng)
    assert noise.std() == pytest.approx(0.15, rel=0.02)
    assert np.corrcoef(noise[:-1], noise[1:])[0, 1] == pytest.approx(0.4, abs=0.01)
