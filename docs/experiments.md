# Experiments

An experiment asks one question of your own data, such as "does a noon caffeine cutoff raise my next-morning HRV?". Each day is randomly assigned to treatment or control. The plan is fixed before any data comes in, and the result is reported as an effect with an honest interval, whatever it turns out to be.

## Running one

1. **Baseline.** Collect three to four weeks of data with nothing running. Then see what an experiment of a given length can detect, given your own night-to-night variation:
   ```sh
   docker compose exec worker whoop-lab power --outcome ln_hrv_rmssd --days 28
   ```
2. **Draft.** On `/experiments`, write the hypothesis, what a treatment day and a control day mean, the primary outcome and the number of assigned days. Options:
   - block size;
   - washout days, which follow each assigned day and are left out of the analysis (for interventions that carry over, such as alcohol);
   - separate randomization for weekdays and weekends.
3. **Lock.** Locking freezes the plan, the analysis and the random seeds. The first assigned day is the next day.
4. **Run.** Each evening after `ASSIGN_HOUR` (20:00 by default), the worker draws tomorrow's arm and sends it through ntfy. `/today` shows the day's arm, asks whether you followed yesterday's, and holds the tag buttons.
5. **Read.** Results are appended daily and shown on the experiment's page and in Grafana. The experiment ends when its planned days are used up, not when the numbers look good.

Run one experiment at a time. Don't start one during illness, travel or an unusual training block; tag those days instead.

## Outcomes

| Outcome | Notes |
| --- | --- |
| `ln_hrv_rmssd` | Natural log of next-morning HRV. The default primary outcome: effects read as percent changes. |
| `resting_heart_rate` | bpm |
| `sleep_efficiency_percentage` | % |
| `deep_sleep_minutes` | minutes |

WHOOP's recovery score is deliberately not an outcome. It is a proprietary composite of the outcomes above, so an effect on it is hard to interpret.

The outcome for day D is the recovery from the main sleep that ends on the morning of D+1 (before 14:00 in `LOCAL_TIMEZONE`). Naps, unscored records and deleted records never count. `LOCAL_TIMEZONE` cannot change while an experiment runs; the stack refuses to start if it does.

## Analysis

- **Primary: intention to treat.** Days are compared by the arm they were assigned, whether or not you complied. The p-value and the 95% interval come from re-drawing the same block randomization 10,000 times. This stays valid at small sample sizes and assumes nothing beyond the randomization itself.
- **Secondary, labelled as such:**
  - a regression adjusted for the prior week's mean of the outcome and that day's strain (with robust HC3 errors);
  - a per-protocol estimate, labelled biased because compliance isn't randomized;
  - the other outcomes, reported as exploratory.
- **Missing mornings** are dropped and counted. If more than 20% are missing, or the two arms differ by more than 3 missing days, the result is marked compromised.

The analysis is checked on simulated experiments before it is trusted. Run `whoop-lab simulate` (about ten seconds). On data with no effect, it must reject 3 to 7% of the time. Its intervals must cover the true effect 93 to 97% of the time. It must also show that an analysis using the wrong block design, or a plain shuffle of the labels, would fail those checks.

This is self-experimentation on everyday habits, not medical advice.
