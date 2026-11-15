"""`whoop-lab` command line.

Commands that call WHOOP are meant to run in the worker container:
    docker compose exec worker whoop-lab backfill
They read the stored token and never refresh it; the worker's schedule does that.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import typer
from sqlalchemy import select, text

from whoop_lab.analysis.inference import FloatArray
from whoop_lab.analysis.pipeline import ResultRow, analyze_experiment, describe
from whoop_lab.analysis.power import minimum_detectable_effect, summarize_baseline
from whoop_lab.analysis.simulate import (
    COVERAGE_BAND,
    MIN_SHUFFLE_GAP,
    REJECTION_BAND,
    validate,
)
from whoop_lab.ble.capture import NOTIFICATIONS_FILE
from whoop_lab.ble.decode import DECODER_VERSION, load_biometrics
from whoop_lab.ble.metrics import METRICS_VERSION, nightly
from whoop_lab.ble.minutes import MINUTES_VERSION, minutes
from whoop_lab.ble.store import (
    describe_files,
    recent_captures,
    since_days,
    store_minutes,
    store_nights,
)
from whoop_lab.config import get_settings
from whoop_lab.db import make_engine, make_session_factory
from whoop_lab.experiments.outcomes import OUTCOMES
from whoop_lab.ingest import sync as sync_module
from whoop_lab.ingest.sync import SyncSummary
from whoop_lab.log import configure_logging
from whoop_lab.models import Experiment
from whoop_lab.setup_db import setup_database
from whoop_lab.whoop.client import WhoopClient

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def _root() -> None:
    """WHOOP archive and n-of-1 experiments."""


async def _with_client(kind: str, since: datetime | None, days: int | None) -> SyncSummary:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.db_url)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            session_factory = make_session_factory(engine)
            client = WhoopClient(http, session_factory, settings)
            if kind == "backfill":
                return await sync_module.backfill(client, session_factory, since=since)
            return await sync_module.reconcile(
                client,
                session_factory,
                window_days=days or settings.reconcile_window_days,
                kind="manual",
            )
    finally:
        await engine.dispose()


def _report(summary: SyncSummary) -> None:
    typer.echo(
        f"{summary.kind}: {summary.records_seen} records seen, "
        f"{summary.rows_upserted} upserted, {summary.rows_deleted} soft-deleted"
    )
    for error in summary.errors:
        typer.echo(f"error: {error}", err=True)
    if not summary.ok:
        raise typer.Exit(1)


@app.command()
def backfill(
    since: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Only load records from this UTC date on."),
    ] = None,
) -> None:
    """Load WHOOP history for every collection. Safe to re-run."""
    start = since.replace(tzinfo=UTC) if since else None
    _report(asyncio.run(_with_client("backfill", start, None)))


@app.command()
def sync(
    days: Annotated[int | None, typer.Option(min=1, help="Trailing window in days.")] = None,
) -> None:
    """Run the reconciliation now: poll cycles and re-fetch the trailing window."""
    _report(asyncio.run(_with_client("manual", None, days)))


async def _setup_db() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.db_url)
    try:
        await setup_database(make_session_factory(engine), settings)
    finally:
        await engine.dispose()


@app.command("setup-db")
def setup_db() -> None:
    """Apply settings the database needs (timezone, Grafana role). Runs on every start."""
    asyncio.run(_setup_db())


async def _baseline_values(outcome: str, baseline_days: int) -> FloatArray:
    """The outcome on recent days that were not part of any experiment."""
    settings = get_settings()
    engine = make_engine(settings.db_url)
    try:
        async with make_session_factory(engine)() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT o.{outcome} FROM daily_outcomes o "  # noqa: S608
                        "WHERE o.local_date < (SELECT max(local_date) FROM daily_outcomes) "
                        "AND NOT EXISTS (SELECT 1 FROM assignments a "
                        "                WHERE a.local_date = o.local_date) "
                        "ORDER BY o.local_date DESC LIMIT :days"
                    ),
                    {"days": baseline_days},
                )
            ).scalars()
            values = [float("nan") if v is None else float(v) for v in rows]
    finally:
        await engine.dispose()
    return np.array(values[::-1], dtype=float)


@app.command()
def power(
    outcome: Annotated[str, typer.Option(help="Outcome metric.")] = "ln_hrv_rmssd",
    days: Annotated[int, typer.Option(min=4, help="Planned assigned days.")] = 28,
    block_size: Annotated[int, typer.Option(min=2)] = 4,
    baseline_days: Annotated[int, typer.Option(min=10, help="Recent non-experiment days.")] = 60,
) -> None:
    """Print the minimum detectable effect for a design, from your baseline variability."""
    if outcome not in OUTCOMES:
        raise typer.BadParameter(f"choose one of: {', '.join(OUTCOMES)}")
    if block_size % 2:
        raise typer.BadParameter("block size must be even")
    values = asyncio.run(_baseline_values(outcome, baseline_days))
    try:
        baseline = summarize_baseline(values)
    except ValueError as exc:
        typer.echo(f"Not enough baseline data yet: {exc}.", err=True)
        raise typer.Exit(1) from None
    result = minimum_detectable_effect(baseline, days=days, block_size=block_size)
    info = OUTCOMES[outcome]
    typer.echo(f"Outcome: {info.label} ({info.unit})")
    typer.echo(
        f"Baseline: {baseline.n_values} mornings over {baseline.n_days} days, "
        f"SD {baseline.sigma:.3f}, day-to-day correlation {baseline.phi:.2f}, "
        f"{baseline.missing_rate:.0%} missing"
    )
    typer.echo(f"Design: {days} days, blocks of {block_size}")
    mde = result.minimum_detectable_effect
    if mde is None:
        typer.echo("No effect size reaches 80% power with this design. Plan more days.")
        raise typer.Exit(1)
    line = f"Minimum detectable effect at 80% power: {mde:.3f} {info.unit}"
    line += f" ({mde / baseline.sigma:.2f} baseline SD"
    if info.log_scale:
        line += f", about {np.expm1(mde) * 100:.1f}%"
    typer.echo(line + ")")


async def _analyze(name_or_id: str) -> tuple[str, list[ResultRow]]:
    settings = get_settings()
    engine = make_engine(settings.db_url)
    today = datetime.now(settings.tz).date()
    try:
        async with make_session_factory(engine)() as session, session.begin():
            query = select(Experiment)
            query = (
                query.where(Experiment.id == int(name_or_id))
                if name_or_id.isdigit()
                else query.where(Experiment.name == name_or_id)
            )
            experiment = (await session.execute(query)).scalar_one_or_none()
            if experiment is None:
                raise typer.BadParameter(f"no experiment {name_or_id!r}")
            if experiment.locked_at is None:
                raise typer.BadParameter("that experiment is still a draft")
            return experiment.name, await analyze_experiment(session, experiment, today)
    finally:
        await engine.dispose()


@app.command()
def analyze(experiment: Annotated[str, typer.Argument(help="Experiment name or id.")]) -> None:
    """Compute results now and append them to experiment_results."""
    name, rows = asyncio.run(_analyze(experiment))
    typer.echo(name)
    if not rows:
        typer.echo("No assigned day has reached its outcome morning yet.")
        return
    for row in rows:
        label = row.details.get("label", "")
        typer.echo(f"\n{OUTCOMES[row.outcome_metric].label} · {row.method} [{label}]")
        typer.echo(f"  {describe(row)}")
        if "skipped" in row.details:
            typer.echo(f"  not computed: {row.details['skipped']}")


@app.command()
def simulate(
    sims: Annotated[
        int, typer.Option(min=2000, help="Simulated experiments per check (2,000 or more).")
    ] = 2000,
    phi: Annotated[
        float,
        typer.Option(
            min=0.3,
            max=0.6,
            help="AR(1) autocorrelation of the noise. Without autocorrelation a wrong "
            "test cannot be told from the right one, so lower values are refused.",
        ),
    ] = 0.4,
) -> None:
    """Validate the analysis on simulated data. Run before trusting any analysis change."""
    report = validate(n_sims=sims, phi=phi)
    low, high = REJECTION_BAND
    checks = [
        (
            "Null calibration",
            report.null_calibrated,
            f"rejection rate {report.null_rejection_rate:.3f} at alpha 0.05 (want {low} to {high})",
        ),
        (
            "Effect recovery",
            report.effect_recovered,
            f"mean estimate {report.mean_estimate:.4f} for a planted {report.planted_effect:.4f}; "
            f"interval coverage {report.coverage:.3f} (want {COVERAGE_BAND[0]} to "
            f"{COVERAGE_BAND[1]})",
        ),
        (
            "Design match",
            report.design_matched,
            f"with the wrong block design the rejection rate is "
            f"{report.mismatch_rejection_rate:.3f} (must fall outside {low} to {high})",
        ),
        (
            "Plain shuffle told apart",
            report.shuffle_told_apart,
            f"a plain shuffle of labels rejects {report.shuffle_rejection_rate:.3f} on the same "
            f"experiments (must be at least {MIN_SHUFFLE_GAP} below the real test)",
        ),
    ]
    checks += [
        (
            f"Scenario: {scenario.name}",
            scenario.passed,
            f"rejection rate {scenario.null_rejection_rate:.3f}; mean estimate "
            f"{scenario.mean_estimate:.4f}; coverage {scenario.coverage:.3f}",
        )
        for scenario in report.scenarios
    ]
    for title, ok, detail in checks:
        typer.echo(f"{'PASS' if ok else 'FAIL'}  {title}: {detail}")
    if not report.passed:
        raise typer.Exit(1)


def _fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


@app.command("ble-nights")
def ble_nights(
    captures: Annotated[
        list[Path], typer.Argument(help="Capture directories or notifications.jsonl files.")
    ],
    timezone: Annotated[str, typer.Option(help="Zone that defines a night.")] = "America/New_York",
) -> None:
    """Nightly metrics from Bluetooth captures. Needs no database; prints to the terminal only."""
    files = [path / NOTIFICATIONS_FILE if path.is_dir() else path for path in captures]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise typer.BadParameter(f"not found: {', '.join(missing)}")
    tz = ZoneInfo(timezone)
    records = load_biometrics(files)
    typer.echo(f"{len(records)} per-second records; decoder {DECODER_VERSION}; {METRICS_VERSION}")
    nights = nightly(records, tz)
    if not nights:
        typer.echo("No night with a sleep period ending before 14:00 in these captures.")
        return
    for night in nights:
        start = datetime.fromtimestamp(night.sleep_start_unix, tz)
        end = datetime.fromtimestamp(night.sleep_end_unix, tz)
        coverage = "n/a" if night.rr_coverage is None else f"{night.rr_coverage:.0%}"
        typer.echo(f"\nNight ending {night.wake_date}: asleep {start:%H:%M} to {end:%H:%M}")
        typer.echo(f"  beats with an R-R interval: {coverage}")
        if night.rr_corrected_share is not None:
            verdict = "ok" if night.rr_quality_ok else "POOR: above 5%, treat HRV with caution"
            typer.echo(f"  intervals corrected: {night.rr_corrected_share:.1%} ({verdict})")
        typer.echo(
            f"  RMSSD: sleep window {_fmt(night.rmssd_sleep_ms)} ms; "
            f"02:00-06:00 {_fmt(night.rmssd_fixed_ms)} ms; "
            f"median of {night.rmssd_segments} five-minute segments "
            f"{_fmt(night.rmssd_segment_median_ms)} ms"
        )
        typer.echo(
            f"  resting heart rate: median {_fmt(night.resting_hr_median)} bpm; "
            f"lowest five minutes {_fmt(night.resting_hr_lowest_segment)} bpm"
        )
        typer.echo(
            f"  respiratory rate: {_fmt(night.respiratory_rate)} per minute "
            f"({night.respiratory_windows} windows)"
        )
        typer.echo(
            f"  blood oxygen (unconfirmed field): {_fmt(night.spo2_mean)}% "
            f"({night.spo2_windows} windows)"
        )
        typer.echo(f"  skin temperature: {_fmt(night.skin_temp_mean_c, 2)} C")


async def _ble_import(captures_dir: Path, days: int | None) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    tz = ZoneInfo(settings.local_timezone)
    files = recent_captures(captures_dir, since_days(days), tz)
    typer.echo(f"captures read: {describe_files(files)}")
    records = load_biometrics(files)
    nights = nightly(records, tz)
    summaries = minutes(records)
    engine = make_engine(settings.db_url)
    try:
        session_factory = make_session_factory(engine)
        stored = await store_nights(session_factory, nights, DECODER_VERSION)
        stored_minutes = await store_minutes(session_factory, summaries, DECODER_VERSION)
    finally:
        await engine.dispose()
    dates = ", ".join(str(night.wake_date) for night in nights) or "none"
    typer.echo(
        f"{len(records)} per-second records; {METRICS_VERSION}; nights found: {dates}; "
        f"rows written: {stored}"
    )
    typer.echo(f"{MINUTES_VERSION}: {len(summaries)} minutes found; rows written: {stored_minutes}")


@app.command("ble-import")
def ble_import(
    captures_dir: Annotated[
        Path, typer.Option(help="Directory holding the capture directories.")
    ] = Path("/captures"),
    days: Annotated[int, typer.Option(help="Read captures taken in the last N days.")] = 3,
    everything: Annotated[
        bool, typer.Option("--all", help="Read every capture, to recompute all nights.")
    ] = False,
) -> None:
    """Compute nightly and per-minute metrics from Bluetooth captures and store them."""
    if not captures_dir.is_dir():
        raise typer.BadParameter(f"not a directory: {captures_dir}")
    asyncio.run(_ble_import(captures_dir, None if everything else days))


def main() -> None:
    app()
