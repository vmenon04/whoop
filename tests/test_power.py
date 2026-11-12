from __future__ import annotations

import numpy as np
import pytest
from typer.testing import CliRunner

from whoop_lab.analysis.power import Baseline, minimum_detectable_effect, summarize_baseline
from whoop_lab.analysis.simulate import ar1_noise
from whoop_lab.cli import app as cli_app


def test_baseline_summary_estimates_sd_autocorrelation_and_missingness() -> None:
    rng = np.random.default_rng(0)
    values = 3.9 + ar1_noise(4000, 0.4, 0.15, rng)
    values[rng.random(4000) < 0.1] = np.nan
    baseline = summarize_baseline(values)
    assert baseline.sigma == pytest.approx(0.15, rel=0.05)
    assert baseline.phi == pytest.approx(0.4, abs=0.05)
    assert baseline.missing_rate == pytest.approx(0.1, abs=0.02)


def test_too_little_baseline_is_an_error() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        summarize_baseline(np.array([3.9, 4.0, np.nan, 4.1]))


def test_detectable_effect_shrinks_with_more_days_and_matches_the_rule_of_thumb() -> None:
    baseline = Baseline(n_values=40, n_days=42, sigma=0.15, phi=0.4, missing_rate=0.05)
    by_days = {
        days: minimum_detectable_effect(baseline, days=days).minimum_detectable_effect
        for days in (14, 28, 56)
    }
    assert all(value is not None for value in by_days.values())
    assert by_days[14] > by_days[28] > by_days[56]  # type: ignore[operator]
    # About one night-to-night standard deviation at 14 days per arm.
    assert by_days[28] == pytest.approx(0.15, rel=0.25)


def test_detectable_effect_scales_with_noise() -> None:
    quiet = Baseline(40, 42, 0.10, 0.4, 0.0)
    noisy = Baseline(40, 42, 0.20, 0.4, 0.0)
    a = minimum_detectable_effect(quiet, days=28, seed=1).minimum_detectable_effect
    b = minimum_detectable_effect(noisy, days=28, seed=1).minimum_detectable_effect
    assert a is not None
    assert b is not None
    assert b == pytest.approx(2 * a, rel=0.02)


def test_cli_has_every_command() -> None:
    result = CliRunner().invoke(cli_app, ["--help"])
    for command in ("backfill", "sync", "power", "analyze", "simulate", "setup-db"):
        assert command in result.output
