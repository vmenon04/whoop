---
name: experiment-analysis
description: Invariants for the n-of-1 experiment engine. Use when touching randomization, assignments, the daily_outcomes view, analysis, power, simulation or results code, or the experiments, assignments and experiment_results tables.
---

# Experiment analysis

This code produces causal claims from about 30 data points. A bug here does not crash. It produces a plausible wrong number. Every rule below exists to keep the result trustworthy, and none may be relaxed to make a test pass or a refactor cleaner. If a rule seems wrong, stop and ask.

## Invariants

1. **Locked means frozen.** Once `experiments.locked_at` is set, `analysis_spec`, `outcome_metric`, `planned_days`, `block_size` and `washout_days` cannot change. Enforce in the application and with a database trigger.
2. **Assignments are immutable.** After creation only `complied` and `note` may change. Never regenerate, backfill or reorder assignments.
3. **Results are append-only.** `experiment_results` rows are never updated or deleted.
4. **Intention to treat is primary.** Days are compared by assigned arm, whatever happened. Per-protocol is secondary and always labeled biased.
5. **The test mirrors the design.** Randomization inference re-draws assignments with the same permuted-block scheme, block size and strata that produced the real ones. A plain shuffle of labels is wrong for this design.
6. **No optional stopping.** An experiment ends at `planned_days`. No code path closes one early because of the estimate or p-value.
7. **One day mapping.** The outcome for local date D is the recovery tied to the main sleep ending on the morning of D+1. This lives only in the `daily_outcomes` view. No other query re-derives it.
8. **Only scored data.** Rows with `score_state` other than SCORED, soft-deleted rows and naps never enter an outcome.
9. **Seeded and reproducible.** Every random draw takes an explicit seed that is stored. The same inputs give the same result.

## Day mapping details

- Local date uses the configured timezone (default America/New_York), not UTC and not the server's zone.
- Main sleep for D+1 morning = the non-nap sleep whose end falls on local date D+1. If there are several, take the longest.
- Covariates for day D (strain, workouts, tags) come from local date D.
- Test across both DST transitions and across a night with a nap.

## Randomization

- Permuted blocks. Default block size 4, two per arm.
- Each block has its own stored seed. The sequence for a block is a function of that seed only.
- Optional stratification by weekday versus weekend: separate block streams per stratum.
- Washout days after a treatment day get no assignment row and are excluded from analysis.
- Tomorrow's arm is assigned the evening before and never earlier than that. Future assignments are not visible in the UI.

## Primary analysis

Outcome default: natural log of next-morning HRV (RMSSD, ms). Effects on the log scale read as approximate percent changes. Report `exp(effect) - 1` as the percent change alongside the raw estimate.

Statistic: mean(outcome | assigned treatment) minus mean(outcome | assigned control), over days with a scored outcome.

Reference implementation of the test. Keep the production version equivalent to this:

```python
import numpy as np

def draw_blocks(n_days: int, block_size: int, rng: np.random.Generator) -> np.ndarray:
    """One assignment vector of length n_days from the permuted-block scheme."""
    base = np.array([1] * (block_size // 2) + [0] * (block_size // 2))
    n_blocks = -(-n_days // block_size)
    return np.concatenate([rng.permutation(base) for _ in range(n_blocks)])[:n_days]

def randomization_test(y, arm, block_size=4, draws=10_000, seed=0, tau=0.0):
    """Two-sided p-value for H0: constant additive effect == tau.

    y: outcomes by day, NaN where missing. arm: 0/1 assigned arm by day.
    Missing days stay in the assignment vector and are masked after the draw,
    so the re-draws see the same missingness pattern as the real experiment.
    """
    y, arm = np.asarray(y, float), np.asarray(arm, int)
    ok = ~np.isnan(y)
    y0 = y - tau * arm                      # outcomes under control, if H0 holds
    def stat(a):
        t, c = ok & (a == 1), ok & (a == 0)
        if not t.any() or not c.any():
            return np.nan
        return (y0 + tau * a)[t].mean() - (y0 + tau * a)[c].mean()
    observed = stat(arm)
    rng = np.random.default_rng(seed)
    sims = np.array([stat(draw_blocks(len(y), block_size, rng)) for _ in range(draws)])
    sims = sims[~np.isnan(sims)]
    # center both on tau so the comparison is two-sided around the null
    p = (1 + np.sum(np.abs(sims - tau) >= abs(observed - tau) - 1e-12)) / (1 + len(sims))
    return observed, p
```

- **Interval.** 95% interval = all `tau` with p > 0.05. Find each end by bisection on `tau`, reusing the same seed for every evaluation.
- **Stratified designs.** Draw blocks per stratum, exactly as the assigner does.
- **Washout.** Washout days are removed before the test and the block structure is rebuilt from the stored `block_index`, not inferred from dates.

## Secondary analyses

- Covariate-adjusted: OLS of outcome on arm, the prior 7-day mean of the outcome (days before D only, never including D+1), and day strain. Report with HC3 standard errors. Label it secondary.
- Per-protocol: restrict to `complied = true`. Label it biased.
- Secondary outcomes (resting HR, sleep efficiency, deep-sleep minutes) are reported without multiplicity correction and labeled exploratory.

## Missing data

- Drop days without a scored outcome. Report the count per arm.
- If more than 20% of assigned days are missing, or missingness differs between arms by more than 3 days, flag the experiment as compromised in the result row.
- Never impute outcomes.

## Power

`whoop-lab power` uses the baseline period's standard deviation of the outcome and simulates the actual design (blocks, planned days, expected missingness) to print the minimum detectable effect at 80% power. Rough guide: 14 days per arm detects about one night-to-night standard deviation.

## Validation, required before any analysis change is committed

Run `whoop-lab simulate` (or the matching pytest marker) and confirm all three:

1. **Null calibration.** With no effect and AR(1) noise (phi around 0.3 to 0.5), the rejection rate at alpha 0.05 is between 0.03 and 0.07 over at least 2,000 simulated experiments.
2. **Effect recovery.** With a planted additive effect, the mean estimate is within 10% of it, and the 95% interval covers it in 93% to 97% of simulations.
3. **Design match.** Changing the simulator's block size without changing the test's makes check 1 fail. This confirms the test is tied to the design.

If any check fails, the change is wrong, even if every unit test is green.

## Never

- Never add a "re-randomize" or "skip today" feature.
- Never choose the outcome, covariates or exclusions after seeing results.
- Never drop outliers by rule of thumb. Exclusions are tagged days decided before unblinding, recorded in `analysis_spec`.
- Never show the user tomorrow's arm early or the remaining block sequence.
- Never present the WHOOP recovery score as the primary outcome.
- Never describe a result as proving anything. Report the estimate, the interval, n per arm and the missing count.
