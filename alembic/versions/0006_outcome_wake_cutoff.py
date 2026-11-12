"""daily_outcomes: a main sleep must end before 14:00 local.

Revision ID: 0006
Revises: 0005

Without a cutoff, when a real night goes unrecorded, an evening doze that WHOOP
does not class as a nap would be taken as that date's main sleep and its
recovery used as the outcome of the day before. A sleep that ends in the
afternoon or evening is now never a main sleep; the day simply has no outcome.

The view's columns are unchanged, so it is replaced in place and keeps its grants.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

WAKE_CUTOFF = "14:00"

DAILY_OUTCOMES = """
CREATE OR REPLACE VIEW daily_outcomes AS
WITH cfg AS (
    SELECT local_timezone AS tz FROM app_config WHERE id = 1
),
-- One main sleep per wake date: not a nap, not deleted, the longest if several.{comment}
main_sleeps AS (
    SELECT DISTINCT ON (wake_date) *
    FROM (
        SELECT s.*, (s."end" AT TIME ZONE cfg.tz)::date AS wake_date
        FROM sleeps s CROSS JOIN cfg
        WHERE NOT s.nap AND s.deleted_at IS NULL{cutoff}
    ) candidates
    ORDER BY wake_date, ("end" - start) DESC, id
),
days AS (
    SELECT day::date AS local_date
    FROM cfg,
         generate_series(
             (SELECT min(wake_date) - 1 FROM main_sleeps)::timestamp,
             (now() AT TIME ZONE cfg.tz)::date::timestamp,
             interval '1 day'
         ) AS day
),
workouts_by_day AS (
    SELECT (w.start AT TIME ZONE cfg.tz)::date AS local_date,
           count(*) AS workout_count,
           max(w.strain) AS workout_max_strain,
           (sum(extract(epoch FROM (w."end" - w.start))) / 60.0)::double precision
               AS workout_minutes
    FROM workouts w CROSS JOIN cfg
    WHERE w.deleted_at IS NULL AND w.score_state = 'SCORED'
    GROUP BY 1
),
tags_by_day AS (
    SELECT local_date, jsonb_object_agg(key, value) AS tags
    FROM daily_tags
    GROUP BY 1
)
SELECT
    d.local_date,
    -- Outcome: the main sleep ending on the morning of D+1 and its scored recovery.
    o.id                                   AS outcome_sleep_id,
    o.start                                AS outcome_sleep_start,
    o."end"                                AS outcome_sleep_end,
    r.sleep_id IS NOT NULL                 AS outcome_scored,
    r.hrv_rmssd_milli,
    CASE WHEN r.hrv_rmssd_milli > 0 THEN ln(r.hrv_rmssd_milli) END AS ln_hrv_rmssd,
    r.resting_heart_rate,
    r.recovery_score,
    r.spo2_percentage,
    r.skin_temp_celsius,
    r.user_calibrating,
    so.sleep_efficiency_percentage,
    (so.total_slow_wave_sleep_time_milli / 60000.0)::double precision AS deep_sleep_minutes,
    so.sleep_performance_percentage,
    ((so.total_in_bed_time_milli - so.total_awake_time_milli) / 60000.0)::double precision
                                           AS asleep_minutes,
    so.respiratory_rate,
    -- Covariates for day D itself.
    c.id                                   AS day_cycle_id,
    c.strain                               AS day_strain,
    c.kilojoule                            AS day_kilojoule,
    c.average_heart_rate                   AS day_average_heart_rate,
    coalesce(w.workout_count, 0)           AS workout_count,
    w.workout_max_strain,
    coalesce(w.workout_minutes, 0.0)       AS workout_minutes,
    coalesce(t.tags, '{}'::jsonb)          AS tags
FROM days d
LEFT JOIN main_sleeps o ON o.wake_date = d.local_date + 1
LEFT JOIN recoveries r
       ON r.sleep_id = o.id AND r.deleted_at IS NULL AND r.score_state = 'SCORED'
-- Sleep-derived outcomes only from a scored sleep.
LEFT JOIN sleeps so ON so.id = o.id AND so.score_state = 'SCORED'
-- The cycle that began with the sleep ending on the morning of D covers day D.
LEFT JOIN main_sleeps m ON m.wake_date = d.local_date
LEFT JOIN cycles c
       ON c.id = m.cycle_id AND c.deleted_at IS NULL AND c.score_state = 'SCORED'
LEFT JOIN workouts_by_day w ON w.local_date = d.local_date
LEFT JOIN tags_by_day t ON t.local_date = d.local_date
"""

WITH_CUTOFF = DAILY_OUTCOMES.replace(
    "{comment}", "\n-- It must end before the wake cutoff: an evening doze is not a night."
).replace(
    "{cutoff}",
    f"\n          AND (s.\"end\" AT TIME ZONE cfg.tz)::time < time '{WAKE_CUTOFF}'",
)
WITHOUT_CUTOFF = DAILY_OUTCOMES.replace("{comment}", "").replace("{cutoff}", "")


def upgrade() -> None:
    op.execute(WITH_CUTOFF)


def downgrade() -> None:
    op.execute(WITHOUT_CUTOFF)
