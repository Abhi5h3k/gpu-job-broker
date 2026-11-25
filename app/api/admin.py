"""Step 0 (dev/demo convenience, not part of the core requirements): wipe
every job, node, usage segment, and clock tick and start over from a clean
slate, without having to stop the process and delete the SQLite file by
hand.

Restart-safety (requirement 7) is a deliberate design feature -- state
survives a process restart on purpose. That's great for a real deployment,
but it means a developer/demo poking at the Swagger UI has no way to get
back to an empty database short of killing the process and deleting
``data/broker.db``. This endpoint is that reset button, exposed safely
over the API instead.
"""

from __future__ import annotations

import random
from datetime import datetime

from fastapi import APIRouter, Request

from app.clock import SimulatedClock, save_capacity_blocked, save_clock, save_heartbeat_anchor
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Base
from app.provider import FakeCloudProvider
from app.schemas import ResetResponse

router = APIRouter(prefix="/admin", tags=["0. Admin"])


@router.post(
    "/reset",
    response_model=ResetResponse,
    summary="Wipe all data and start over",
    description=(
        "Drops and recreates every table (jobs, nodes, usage segments, job events, clock "
        "state) and resets the simulated clock and fake provider to a fresh start -- "
        "equivalent to stopping the process and deleting the database file, but safe to call "
        "from Swagger while the server keeps running. Intended for development and demos, not "
        "a real deployment: there is no confirmation step and no undo."
    ),
)
def reset(request: Request) -> ResetResponse:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    now = datetime.utcnow()
    session = SessionLocal()
    try:
        clock = SimulatedClock(now)
        save_clock(session, clock)
        save_heartbeat_anchor(session, now)
        save_capacity_blocked(session, False, now)
    finally:
        session.close()

    # Fresh process-wide clock/provider too, so a reset really does put the
    # app back in the exact state it would be in on a brand-new start --
    # otherwise the provider's RNG stream would keep going from wherever it
    # was, and successive resets would stop being reproducible.
    request.app.state.clock = clock
    request.app.state.provider = FakeCloudProvider(rng=random.Random(settings.random_seed))

    return ResetResponse(reset_at=now)
