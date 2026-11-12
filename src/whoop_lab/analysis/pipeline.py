"""Turn an experiment's assignments and outcomes into result rows.

Primary: intention to treat, difference in means by ASSIGNED arm, p-value and
interval by randomization inference that mirrors the block design.
Secondary (always labelled): covariate-adjusted OLS, per-protocol (biased), and
exploratory secondary outcomes.

Outcomes come from the daily_outcomes view and nowhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.analysis.inference import BlockDesign, RandomizationTest
from whoop_lab.experiments.outcomes import OUTCOMES
from whoop_lab.models.experiments import ARM_TREATMENT, Experiment, ExperimentResult

METHOD_PRIMARY = "itt_randomization"
METHOD_EXPLORATORY = "itt_randomization_exploratory"
METHOD_ADJUSTED = "ols_adjusted_hc3_secondary"
METHOD_PER_PROTOCOL = "per_protocol_biased"

PRIOR_WINDOW_DAYS = 7
PRIOR_MIN_VALUES = 3
OLS_MIN_ROWS = 8

_OUTCOME_COLUMNS = ", ".join(f"o.{key}" for key in OUTCOMES)


@dataclass
class ResultRow:
    outcome_metric: str
    method: str
    n_treatment: int
    n_control: int
    n_missing_treatment: int
    n_missing_control: int
    effect: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    compromised: bool
    details: dict[str, Any] = field(default_factory=dict)


def _finite(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else float(value)


async def load_frame(session: AsyncSession, experiment: Experiment) -> pd.DataFrame:
    """One row per assigned day with its outcomes and covariates, oldest first.

    Adds `prior_<metric>`: the mean of that outcome over the seven days before D
    (D-7 .. D-1), which never includes day D's own outcome.
    """
    rows = (
        await session.execute(
            text(
                f"SELECT a.local_date, a.arm, a.stratum, a.block_index, a.block_position, "  # noqa: S608
                f"a.complied, coalesce(o.outcome_scored, false) AS outcome_scored, "
                f"o.day_strain, {_OUTCOME_COLUMNS} "
                "FROM assignments a LEFT JOIN daily_outcomes o USING (local_date) "
                "WHERE a.experiment_id = :id ORDER BY a.local_date"
            ),
            {"id": experiment.id},
        )
    ).mappings()
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty:
        return frame
    for key in (*OUTCOMES, "day_strain"):
        frame[key] = pd.to_numeric(frame[key], errors="coerce").astype(float)

    first = min(frame["local_date"]) - timedelta(days=PRIOR_WINDOW_DAYS)
    history_rows = (
        await session.execute(
            text(
                f"SELECT o.local_date, {_OUTCOME_COLUMNS} FROM daily_outcomes o "  # noqa: S608
                "WHERE o.local_date >= :first AND o.local_date <= :last ORDER BY o.local_date"
            ),
            {"first": first, "last": max(frame["local_date"])},
        )
    ).mappings()
    history = pd.DataFrame([dict(row) for row in history_rows])
    for key in OUTCOMES:
        priors: list[float] = []
        series = (
            pd.to_numeric(history.set_index("local_date")[key], errors="coerce")
            if not history.empty
            else pd.Series(dtype=float)
        )
        for day in frame["local_date"]:
            window = [
                series.get(day - timedelta(days=back)) for back in range(1, PRIOR_WINDOW_DAYS + 1)
            ]
            values = [float(v) for v in window if v is not None and not pd.isna(v)]
            priors.append(float(np.mean(values)) if len(values) >= PRIOR_MIN_VALUES else np.nan)
        frame[f"prior_{key}"] = priors
    return frame


def _percent(outcome_key: str, value: float | None) -> float | None:
    """On a log scale an effect e is a ratio of exp(e): report exp(e) - 1 as a percent change."""
    if value is None or not OUTCOMES[outcome_key].log_scale:
        return None
    return _finite(math.expm1(value) * 100.0)


def _itt(
    due: pd.DataFrame,
    experiment: Experiment,
    outcome_key: str,
    method: str,
    seed: int,
    *,
    mask: pd.Series | None = None,
) -> ResultRow:
    spec = experiment.analysis_spec
    alpha = float(spec.get("alpha", 0.05))
    y = due[outcome_key].to_numpy(dtype=float, copy=True)
    if mask is not None:
        y[~mask.to_numpy(dtype=bool)] = np.nan
    arm = (due["arm"] == ARM_TREATMENT).to_numpy(dtype=int)
    design = BlockDesign.from_assignments(
        due["stratum"].tolist(),
        due["block_index"].astype(int).tolist(),
        due["block_position"].astype(int).tolist(),
        experiment.block_size,
    )
    result = RandomizationTest(
        y, arm, design, draws=int(spec.get("draws", 10_000)), seed=seed
    ).result(alpha)

    outcome_missing = due[outcome_key].isna().to_numpy()
    missing_treatment = int((outcome_missing & (arm == 1)).sum())
    missing_control = int((outcome_missing & (arm == 0)).sum())
    n_due = len(due)
    missing_fraction = (missing_treatment + missing_control) / n_due if n_due else 0.0
    compromised = missing_fraction > float(spec.get("max_missing_fraction", 0.20)) or abs(
        missing_treatment - missing_control
    ) > int(spec.get("max_missing_imbalance_days", 3))
    return ResultRow(
        outcome_metric=outcome_key,
        method=method,
        n_treatment=result.n_treatment,
        n_control=result.n_control,
        n_missing_treatment=missing_treatment,
        n_missing_control=missing_control,
        effect=_finite(result.effect),
        ci_low=_finite(result.ci_low),
        ci_high=_finite(result.ci_high),
        p_value=_finite(result.p_value),
        compromised=compromised,
        details={
            "alpha": alpha,
            "draws": result.draws,
            "exact": result.exact,
            "seed": seed,
            "days_due": n_due,
            "missing_fraction": round(missing_fraction, 4),
            "percent_change": _percent(outcome_key, result.effect),
            "percent_ci_low": _percent(outcome_key, result.ci_low),
            "percent_ci_high": _percent(outcome_key, result.ci_high),
        },
    )


def _adjusted(due: pd.DataFrame, primary: ResultRow, outcome_key: str, alpha: float) -> ResultRow:
    """OLS of the outcome on arm, the prior 7-day mean of the outcome, and day strain,
    with HC3 standard errors and t quantiles. Secondary.

    The model knows nothing about the blocks. Blocking makes the real estimate
    more precise than this model assumes, so its interval tends to be wider than
    it needs to be, and it is not a way to get a narrower interval than the
    primary analysis.
    """
    data = pd.DataFrame(
        {
            "y": due[outcome_key],
            "arm": (due["arm"] == ARM_TREATMENT).astype(float),
            "prior_7d_mean": due[f"prior_{outcome_key}"],
            "day_strain": due["day_strain"],
        }
    )
    have_outcome = int(data["y"].notna().sum())
    complete = data.dropna()
    n_treatment = int((complete["arm"] == 1).sum())
    n_control = int((complete["arm"] == 0).sum())
    details: dict[str, Any] = {
        "label": "secondary",
        "covariates": ["prior_7d_mean", "day_strain"],
        "standard_errors": "HC3",
        "quantiles": "t",
        "alpha": alpha,
        "ignores_blocks": True,
        "rows_dropped_for_missing_covariates": have_outcome - len(complete),
        "caveat": (
            "day_strain is measured after assignment; if the intervention changes "
            "activity, adjusting for it biases this estimate"
        ),
    }
    row = ResultRow(
        outcome_metric=outcome_key,
        method=METHOD_ADJUSTED,
        n_treatment=n_treatment,
        n_control=n_control,
        n_missing_treatment=primary.n_missing_treatment,
        n_missing_control=primary.n_missing_control,
        effect=None,
        ci_low=None,
        ci_high=None,
        p_value=None,
        compromised=primary.compromised,
        details=details,
    )
    if len(complete) < OLS_MIN_ROWS or n_treatment < 2 or n_control < 2:
        details["skipped"] = "too few complete rows"
        return row
    try:
        design = sm.add_constant(
            complete[["arm", "prior_7d_mean", "day_strain"]], has_constant="add"
        )
        fit = sm.OLS(complete["y"], design).fit(cov_type="HC3", use_t=True)
        low, high = fit.conf_int(alpha=alpha).loc["arm"]
        row.effect = _finite(float(fit.params["arm"]))
        row.ci_low, row.ci_high = _finite(float(low)), _finite(float(high))
        row.p_value = _finite(float(fit.pvalues["arm"]))
    except (np.linalg.LinAlgError, ValueError, ZeroDivisionError) as exc:
        details["skipped"] = f"model could not be fitted ({type(exc).__name__})"
        return row
    if row.effect is None or row.ci_low is None or row.ci_high is None:
        row.effect = row.ci_low = row.ci_high = row.p_value = None
        details["skipped"] = "model was degenerate"
    else:
        details["percent_change"] = _percent(outcome_key, row.effect)
    return row


def due_mask(frame: pd.DataFrame, today: date) -> pd.Series:
    """Which assigned days are ready to analyse.

    Day D's outcome arrives on the morning of D+1. D is due once that whole
    morning has passed (D+1 < today), or earlier if its recovery is already
    scored. Yesterday's day with no scored recovery yet is not due: the score
    may still be on its way, and results are append-only, so counting it as
    missing now would leave a wrong row behind for good. Days not due are
    neither analysed nor counted as missing.
    """
    yesterday = today - timedelta(days=1)
    settled = frame["local_date"] < yesterday
    scored = (frame["local_date"] == yesterday) & frame["outcome_scored"].astype(bool)
    return settled | scored


def analyze_frame(frame: pd.DataFrame, experiment: Experiment, today: date) -> list[ResultRow]:
    """Compute every result row from an already loaded frame. Pure: no database access."""
    spec = experiment.analysis_spec
    primary_key = str(spec.get("primary_outcome", experiment.outcome_metric))
    seed = int(spec.get("seed", 0))
    if frame.empty:
        return []
    due = frame[due_mask(frame, today)].reset_index(drop=True)
    if due.empty:
        return []

    primary = _itt(due, experiment, primary_key, METHOD_PRIMARY, seed)
    primary.details["label"] = "primary"
    rows = [primary, _adjusted(due, primary, primary_key, float(spec.get("alpha", 0.05)))]

    complied = due["complied"].map(lambda value: value is True)
    per_protocol = _itt(due, experiment, primary_key, METHOD_PER_PROTOCOL, seed + 1, mask=complied)
    per_protocol.details.update(
        {
            "label": "secondary, biased",
            "caveat": "restricted to days marked complied; compliance is not randomized",
            "days_not_complied": int(due["complied"].map(lambda value: value is False).sum()),
            "days_compliance_unrecorded": int(due["complied"].isna().sum()),
        }
    )
    rows.append(per_protocol)

    for offset, key in enumerate(spec.get("secondary_outcomes", []), start=2):
        if key not in OUTCOMES or key == primary_key:
            continue
        row = _itt(due, experiment, key, METHOD_EXPLORATORY, seed + offset)
        row.details["label"] = "exploratory, no multiplicity correction"
        rows.append(row)
    return rows


async def analyze_experiment(
    session: AsyncSession, experiment: Experiment, today: date
) -> list[ResultRow]:
    """Compute results and append them to experiment_results. Never updates old rows."""
    frame = await load_frame(session, experiment)
    rows = analyze_frame(frame, experiment, today)
    for row in rows:
        session.add(
            ExperimentResult(
                experiment_id=experiment.id,
                outcome_metric=row.outcome_metric,
                method=row.method,
                n_treatment=row.n_treatment,
                n_control=row.n_control,
                n_missing_treatment=row.n_missing_treatment,
                n_missing_control=row.n_missing_control,
                effect=row.effect,
                ci_low=row.ci_low,
                ci_high=row.ci_high,
                p_value=row.p_value,
                compromised=row.compromised,
                details=row.details,
            )
        )
    await session.flush()
    return rows


def _number(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}"


def describe(row: ResultRow | ExperimentResult) -> str:
    """One plain sentence: estimate, interval, n per arm, missing count. No verdicts."""
    outcome = OUTCOMES.get(row.outcome_metric)
    unit = outcome.unit if outcome else ""
    parts = [f"estimate {_number(row.effect)} {unit}".rstrip()]
    percent = row.details.get("percent_change")
    if percent is not None:
        parts[0] += f" (about {percent:+.1f}%)"
    level = f"{(1 - float(row.details.get('alpha', 0.05))) * 100:g}%"
    if row.ci_low is not None and row.ci_high is not None:
        parts.append(f"{level} interval {_number(row.ci_low)} to {_number(row.ci_high)}")
    elif row.effect is not None:
        parts.append(f"{level} interval not bounded yet")
    if row.p_value is not None:
        parts.append(f"p = {row.p_value:.3f}")
    parts.append(f"n = {row.n_treatment} treatment, {row.n_control} control")
    missing = row.n_missing_treatment + row.n_missing_control
    parts.append(f"{missing} mornings missing")
    text_ = "; ".join(parts)
    if row.compromised:
        text_ += "; COMPROMISED by missing data"
    return text_
