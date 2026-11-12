"""/experiments: list, create a draft, lock, and read the results summary."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.analysis.pipeline import METHOD_PRIMARY, describe
from whoop_lab.api.deps import local_today, same_origin
from whoop_lab.api.templating import templates
from whoop_lab.experiments import service
from whoop_lab.experiments.outcomes import OUTCOMES
from whoop_lab.experiments.service import Draft, ExperimentError
from whoop_lab.models import Assignment, Experiment, ExperimentResult

router = APIRouter()

METHOD_LABELS = {
    "itt_randomization": "Primary: intention to treat, randomization test",
    "ols_adjusted_hc3_secondary": "Secondary: adjusted for prior 7-day mean and day strain",
    "per_protocol_biased": "Secondary: per protocol (biased; compliance is not randomized)",
    "itt_randomization_exploratory": "Exploratory outcome, no multiplicity correction",
}


async def _latest_results(session: AsyncSession, experiment_id: int) -> list[ExperimentResult]:
    latest = await session.scalar(
        select(func.max(ExperimentResult.computed_at)).where(
            ExperimentResult.experiment_id == experiment_id
        )
    )
    if latest is None:
        return []
    return list(
        (
            await session.execute(
                select(ExperimentResult)
                .where(
                    ExperimentResult.experiment_id == experiment_id,
                    ExperimentResult.computed_at == latest,
                )
                .order_by(ExperimentResult.id)
            )
        ).scalars()
    )


def _result_view(result: ExperimentResult) -> dict[str, Any]:
    return {
        "outcome": OUTCOMES[result.outcome_metric].label
        if result.outcome_metric in OUTCOMES
        else result.outcome_metric,
        "method": METHOD_LABELS.get(result.method, result.method),
        "primary": result.method == METHOD_PRIMARY,
        "sentence": describe(result),
        "skipped": result.details.get("skipped"),
        "caveat": result.details.get("caveat"),
        "compromised": result.compromised,
        "computed_at": result.computed_at,
    }


async def _render_list(request: Request, *, error: str = "", status: int = 200) -> Response:
    async with request.app.state.session_factory() as session:
        experiments = list(
            (await session.execute(select(Experiment).order_by(Experiment.id.desc()))).scalars()
        )
        cards = []
        for experiment in experiments:
            results = await _latest_results(session, experiment.id)
            primary = next((r for r in results if r.method == METHOD_PRIMARY), None)
            cards.append(
                {
                    "experiment": experiment,
                    "assigned": await service.assigned_count(session, experiment.id),
                    "primary": _result_view(primary) if primary else None,
                }
            )
    return templates.TemplateResponse(
        request,
        "experiments.html",
        {
            "cards": cards,
            "outcomes": OUTCOMES,
            "error": error,
            "tz": request.app.state.settings.tz,
        },
        status_code=status,
    )


@router.get("/experiments", response_class=HTMLResponse)
async def list_experiments(request: Request) -> Response:
    return await _render_list(request)


@router.post("/experiments", dependencies=[Depends(same_origin)])
async def create_experiment(
    request: Request,
    name: Annotated[str, Form()],
    hypothesis: Annotated[str, Form()],
    intervention: Annotated[str, Form()],
    control: Annotated[str, Form()],
    planned_days: Annotated[int, Form()],
    outcome_metric: Annotated[str, Form()] = "ln_hrv_rmssd",
    block_size: Annotated[int, Form()] = 4,
    washout_days: Annotated[int, Form()] = 0,
    stratify_weekend: Annotated[str, Form()] = "",
) -> Response:
    draft = Draft(
        name=name,
        hypothesis=hypothesis,
        intervention=intervention,
        control=control,
        planned_days=planned_days,
        outcome_metric=outcome_metric,
        block_size=block_size,
        washout_days=washout_days,
        stratify_weekend=bool(stratify_weekend),
    )
    try:
        async with request.app.state.session_factory() as session, session.begin():
            experiment = await service.create_draft(session, draft)
            experiment_id = experiment.id
    except ExperimentError as exc:
        return await _render_list(request, error=str(exc), status=400)
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.get("/experiments/{experiment_id}", response_class=HTMLResponse)
async def show_experiment(request: Request, experiment_id: int) -> Response:
    async with request.app.state.session_factory() as session:
        experiment = await session.get(Experiment, experiment_id)
        if experiment is None:
            raise HTTPException(404, "no such experiment")
        results = [_result_view(r) for r in await _latest_results(session, experiment_id)]
        # Every stored assignment is for tomorrow at the latest: nothing further ahead exists.
        assignments = list(
            (
                await session.execute(
                    select(Assignment)
                    .where(Assignment.experiment_id == experiment_id)
                    .order_by(Assignment.local_date.desc())
                )
            ).scalars()
        )
    return templates.TemplateResponse(
        request,
        "experiment.html",
        {
            "experiment": experiment,
            "outcome": OUTCOMES.get(experiment.outcome_metric),
            "results": results,
            "assignments": assignments,
            "tz": request.app.state.settings.tz,
        },
    )


@router.post("/experiments/{experiment_id}/lock", dependencies=[Depends(same_origin)])
async def lock(request: Request, experiment_id: int) -> Response:
    try:
        async with request.app.state.session_factory() as session, session.begin():
            await service.lock_experiment(session, experiment_id, local_today(request))
    except ExperimentError as exc:
        return await _render_list(request, error=str(exc), status=400)
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/experiments/{experiment_id}/delete", dependencies=[Depends(same_origin)])
async def delete(request: Request, experiment_id: int) -> Response:
    try:
        async with request.app.state.session_factory() as session, session.begin():
            await service.delete_draft(session, experiment_id)
    except ExperimentError as exc:
        return await _render_list(request, error=str(exc), status=400)
    return RedirectResponse("/experiments", status_code=303)
