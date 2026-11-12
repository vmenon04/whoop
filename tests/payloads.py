"""WHOOP v2 payload builders, shaped like the examples in the OpenAPI spec."""

from __future__ import annotations

from typing import Any

Record = dict[str, Any]

USER_ID = 10129
SLEEP_ID = "ecfc6a15-4661-442f-a9a4-f160dd7afae8"
WORKOUT_ID = "1043c2a0-2c6f-4c3e-9d5a-6f2b8a1c7e11"
CYCLE_ID = 93845


def cycle(cycle_id: int = CYCLE_ID, **overrides: Any) -> Record:
    record: Record = {
        "id": cycle_id,
        "user_id": USER_ID,
        "created_at": "2026-11-10T11:25:44.774Z",
        "updated_at": "2026-11-10T14:25:44.774Z",
        "start": "2026-11-10T02:25:44.774Z",
        "end": "2026-11-11T03:10:00.000Z",
        "timezone_offset": "-05:00",
        "score_state": "SCORED",
        "step_count": 8421,
        "score": {
            "strain": 5.2951527,
            "kilojoule": 8288.297,
            "average_heart_rate": 68,
            "max_heart_rate": 141,
        },
    }
    record.update(overrides)
    return record


def sleep(sleep_id: str = SLEEP_ID, **overrides: Any) -> Record:
    record: Record = {
        "id": sleep_id,
        "cycle_id": CYCLE_ID,
        "v1_id": 93845,
        "user_id": USER_ID,
        "created_at": "2026-11-10T11:25:44.774Z",
        "updated_at": "2026-11-10T14:25:44.774Z",
        "start": "2026-11-10T02:25:44.774Z",
        "end": "2026-11-10T10:25:44.774Z",
        "timezone_offset": "-05:00",
        "nap": False,
        "score_state": "SCORED",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": 30272735,
                "total_awake_time_milli": 1403507,
                "total_no_data_time_milli": 0,
                "total_light_sleep_time_milli": 14905851,
                "total_slow_wave_sleep_time_milli": 6630370,
                "total_rem_sleep_time_milli": 5879573,
                "sleep_cycle_count": 3,
                "disturbance_count": 12,
            },
            "sleep_needed": {
                "baseline_milli": 27395716,
                "need_from_sleep_debt_milli": 352230,
                "need_from_recent_strain_milli": 208595,
                "need_from_recent_nap_milli": -12312,
            },
            "respiratory_rate": 16.11328125,
            "sleep_performance_percentage": 98.0,
            "sleep_consistency_percentage": 90.0,
            "sleep_efficiency_percentage": 91.69533848,
        },
    }
    record.update(overrides)
    return record


def recovery(sleep_id: str = SLEEP_ID, **overrides: Any) -> Record:
    record: Record = {
        "cycle_id": CYCLE_ID,
        "sleep_id": sleep_id,
        "user_id": USER_ID,
        "created_at": "2026-11-10T11:25:44.774Z",
        "updated_at": "2026-11-10T14:25:44.774Z",
        "score_state": "SCORED",
        "score": {
            "user_calibrating": False,
            "recovery_score": 44.0,
            "resting_heart_rate": 64.0,
            "hrv_rmssd_milli": 31.813562,
            "spo2_percentage": 95.6875,
            "skin_temp_celsius": 33.7,
        },
    }
    record.update(overrides)
    return record


def workout(workout_id: str = WORKOUT_ID, **overrides: Any) -> Record:
    record: Record = {
        "id": workout_id,
        "v1_id": 1043,
        "user_id": USER_ID,
        "created_at": "2026-11-10T11:25:44.774Z",
        "updated_at": "2026-11-10T14:25:44.774Z",
        "start": "2026-11-10T22:25:44.774Z",
        "end": "2026-11-10T23:25:44.774Z",
        "timezone_offset": "-05:00",
        "sport_name": "running",
        "sport_id": 1,
        "score_state": "SCORED",
        "score": {
            "strain": 8.2463,
            "average_heart_rate": 123,
            "max_heart_rate": 146,
            "kilojoule": 1569.34033203125,
            "percent_recorded": 100.0,
            "distance_meter": 1772.77035916,
            "altitude_gain_meter": 46.64384460449,
            "altitude_change_meter": -0.781372010707855,
            "zone_durations": {
                "zone_zero_milli": 300000,
                "zone_one_milli": 600000,
                "zone_two_milli": 900000,
                "zone_three_milli": 900000,
                "zone_four_milli": 600000,
                "zone_five_milli": 300000,
            },
        },
    }
    record.update(overrides)
    return record
