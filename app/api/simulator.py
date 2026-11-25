"""Step 3 of the story: this is the simulated clock. Nothing in this
service happens in real time or real sleeps -- time only moves forward
when you call this endpoint. Call it with a small number to step through
a demo one event at a time, or a big number to fast-forward through an
entire scenario instantly."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.broker import Broker
from app.deps import get_broker, get_db_session
from app.schemas import AdvanceRequest, AdvanceResponse

router = APIRouter(prefix="/simulator", tags=["3. Simulator clock"])


def _broker(request: Request, session: Session = Depends(get_db_session)) -> Broker:
    return get_broker(request, session)


@router.post(
    "/advance",
    response_model=AdvanceResponse,
    summary="Move simulated time forward",
    description=(
        "This is how you make anything happen in the broker: nodes becoming ready, jobs "
        "finishing, the idle reaper firing, interruptible nodes getting reclaimed -- none of it "
        "happens on its own. Submit a job, then call this endpoint (try `minutes: 10` to start) "
        "and watch `GET /jobs/{id}` and `GET /nodes` change. The response summarizes everything "
        "that happened during the jump, in order."
    ),
)
def advance(
    payload: AdvanceRequest, broker: Broker = Depends(_broker)
) -> AdvanceResponse:
    summary = broker.advance(payload.minutes)
    return AdvanceResponse(
        now=broker.clock.now,
        nodes_ready=summary.nodes_ready,
        nodes_failed_never_ready=summary.nodes_failed_never_ready,
        nodes_reclaimed=summary.nodes_reclaimed,
        nodes_reaped_idle=summary.nodes_reaped_idle,
        jobs_completed=summary.jobs_completed,
        jobs_requeued_after_reclaim=summary.jobs_requeued_after_reclaim,
        capacity_unavailable_events=summary.capacity_unavailable_events,
    )
