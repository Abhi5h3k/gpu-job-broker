"""Step 4 of the story: see what everything has cost so far, per team,
with idle time and platform overhead broken out separately."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.billing import compute_cost_report
from app.broker import Broker
from app.deps import get_broker, get_db_session
from app.schemas import CostReportOut, TeamCostOut

router = APIRouter(prefix="/costs", tags=["4. Cost report"])


def _broker(request: Request, session: Session = Depends(get_db_session)) -> Broker:
    return get_broker(request, session)


@router.get(
    "",
    response_model=CostReportOut,
    summary="GPU-hours and cost per team for a time window",
    description=(
        "GPU-hours follows the spec literally: GPUs a job requested x hours it actually ran. "
        "The dollar figures come from the node billing ledger and are split into "
        "`running_cost` (the node was doing this team's work) and `idle_cost` (the node was up "
        "but empty, attributed to whoever ran on it most recently). `platform_overhead_cost` is "
        "money spent on nodes that never served anyone's job at all (e.g. a launch that never "
        "became ready) -- see docs/DESIGN.md for why that's not charged to any team.\n\n"
        "Omit `team`, `window_start`, and `window_end` to get the report for every team, over "
        "the whole history so far."
    ),
)
def get_cost_report(
    broker: Broker = Depends(_broker),
    team: str | None = Query(default=None, description="Filter to a single team."),
    window_start: datetime | None = Query(
        default=None, description="Defaults to the epoch (everything so far)."
    ),
    window_end: datetime | None = Query(
        default=None,
        description="Defaults to right now (the simulated clock's current time).",
    ),
) -> CostReportOut:
    start = window_start or datetime.min
    end = window_end or broker.clock.now
    report = compute_cost_report(
        broker.session, start, end, broker.clock.now, team=team
    )
    teams_out = [
        TeamCostOut(
            team=t.team,
            gpu_hours=t.gpu_hours,
            running_cost=t.running_cost,
            idle_cost=t.idle_cost,
            total_cost=t.total_cost,
        )
        for t in report.teams.values()
    ]
    return CostReportOut(
        window_start=report.window_start,
        window_end=report.window_end,
        teams=teams_out,
        platform_overhead_cost=report.platform_overhead_cost,
        total_cost=report.total_cost,
    )
