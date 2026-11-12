"""Outcome metrics an experiment may name. Each is a column of the daily_outcomes view."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    key: str  # also the daily_outcomes column
    label: str
    unit: str
    log_scale: bool = False  # effect reads as an approximate percent change


OUTCOMES: dict[str, Outcome] = {
    o.key: o
    for o in (
        Outcome("ln_hrv_rmssd", "HRV (ln RMSSD)", "ln ms", log_scale=True),
        Outcome("resting_heart_rate", "Resting heart rate", "bpm"),
        Outcome("sleep_efficiency_percentage", "Sleep efficiency", "%"),
        Outcome("deep_sleep_minutes", "Deep sleep", "min"),
    )
}
# The WHOOP recovery score is deliberately absent: it is a proprietary composite
# and must never be a primary outcome.

PRIMARY_DEFAULT = "ln_hrv_rmssd"
SECONDARY_DEFAULT = ("resting_heart_rate", "sleep_efficiency_percentage", "deep_sleep_minutes")


def default_analysis_spec(outcome_metric: str, seed: int) -> dict[str, object]:
    return {
        "primary_outcome": outcome_metric,
        "secondary_outcomes": [key for key in SECONDARY_DEFAULT if key != outcome_metric],
        "estimand": "intention_to_treat",
        "test": "randomization_inference",
        "draws": 10_000,
        "alpha": 0.05,
        "seed": seed,
        "covariates": ["prior_7d_mean", "day_strain"],
        "max_missing_fraction": 0.20,
        "max_missing_imbalance_days": 3,
    }
