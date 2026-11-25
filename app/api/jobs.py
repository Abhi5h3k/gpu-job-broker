"""Step 1 of the story: submit, inspect, cancel, and complete jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.broker import Broker, InvalidJobStateError, JobNotFoundError
from app.deps import get_broker, get_db_session
from app.models import JobState
from app.schemas import CompleteJobRequest, JobDetailOut, JobOut, SubmitJobRequest

router = APIRouter(prefix="/jobs", tags=["1. Jobs"])


def _broker(request: Request, session: Session = Depends(get_db_session)) -> Broker:
    return get_broker(request, session)


@router.post(
    "",
    response_model=JobOut,
    status_code=201,
    summary="Submit a job",
    description=(
        "Queue a new job for your team. Returns immediately with the job in the `queued` "
        "state -- it won't actually start running until you move simulated time forward with "
        "`POST /simulator/advance` (see that endpoint for why).\n\n"
        "**Idempotent:** pass a `client_key` and submitting the exact same key twice returns "
        "the original job instead of creating a duplicate -- safe to retry on a network error."
    ),
)
def submit_job(payload: SubmitJobRequest, broker: Broker = Depends(_broker)) -> JobOut:
    job = broker.submit_job(
        team=payload.team,
        gpus=payload.gpus,
        estimated_minutes=payload.estimated_minutes,
        priority=payload.priority,
        client_key=payload.client_key,
    )
    return JobOut.model_validate(job)


@router.get(
    "/{job_id}",
    response_model=JobDetailOut,
    summary="Get a job's current state and history",
    description=(
        "Returns the job's current fields plus its full state-transition history, so you can "
        "see exactly when it moved from queued to running, and (if it happened) when it was "
        "requeued after an interruptible node got reclaimed."
    ),
)
def get_job(job_id: str, broker: Broker = Depends(_broker)) -> JobDetailOut:
    try:
        job = broker.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}") from exc
    events = _job_events(broker, job_id)
    data = JobOut.model_validate(job).model_dump()
    return JobDetailOut(**data, events=events)


def _job_events(broker: Broker, job_id: str) -> list[dict]:
    from app.models import JobEvent

    events = (
        broker.session.query(JobEvent).filter(JobEvent.job_id == job_id).order_by(JobEvent.at).all()
    )
    return [{"state": e.state, "at": e.at, "note": e.note} for e in events]


@router.post(
    "/{job_id}/cancel",
    response_model=JobOut,
    summary="Cancel a job",
    description="Cancel a job while it's `queued` or `running`. Already-finished jobs can't be cancelled.",
)
def cancel_job(job_id: str, broker: Broker = Depends(_broker)) -> JobOut:
    try:
        job = broker.cancel_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}") from exc
    except InvalidJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobOut.model_validate(job)


@router.post(
    "/{job_id}/complete",
    response_model=JobOut,
    summary="Manually end a running job",
    description=(
        "Most jobs finish on their own once enough simulated time has passed (see "
        "`POST /simulator/advance`). Use this endpoint instead when you want to end a running "
        "job early, or force a `failed` outcome for a scenario the simulator wouldn't otherwise "
        "produce on its own."
    ),
)
def complete_job(
    job_id: str, payload: CompleteJobRequest, broker: Broker = Depends(_broker)
) -> JobOut:
    outcome = JobState.SUCCEEDED if payload.outcome == "succeeded" else JobState.FAILED
    try:
        job = broker.complete_job(job_id, outcome)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}") from exc
    except InvalidJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobOut.model_validate(job)


@router.get(
    "",
    response_model=list[JobOut],
    summary="List the queue, or filter jobs by state/team",
    description=(
        "With no filters, returns queued jobs in the exact order the scheduler would place "
        "them next -- priority and aging (and, under the default Balanced policy, a per-team "
        "fairness tiebreaker) all factor into this order. See docs/DESIGN.md for the full "
        "fairness policy.\n\n"
        "Pass `state` (repeatable) and/or `team` to instead search *all* jobs regardless of "
        "state -- e.g. `?state=succeeded&state=failed` to see everything that's finished, or "
        "`?team=vision` for one team's full history. Filtered results are ordered by "
        "submission time (oldest first), not scheduler order, since that ordering only applies "
        "to jobs still waiting in the queue."
    ),
)
def list_queue(
    state: list[JobState] | None = Query(
        default=None, description="Filter to one or more job states."
    ),
    team: str | None = Query(default=None, description="Filter to a single team."),
    broker: Broker = Depends(_broker),
) -> list[JobOut]:
    if state or team:
        jobs = broker.list_jobs(states=state, team=team)
    else:
        jobs = broker.list_queue()
    return [JobOut.model_validate(j) for j in jobs]
