"""The FastAPI app: wires up the database, the process-wide simulated
clock and fake provider, and every router. Start here:

    uv run uvicorn app.main:app --reload

then open http://127.0.0.1:8000/ (redirects to /guide) -- a narrated,
single-page walkthrough with diagrams and runnable "try it" examples.
http://127.0.0.1:8000/docs is the detailed Swagger reference for after.
"""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api import costs, jobs, nodes, retrieval, simulator
from app.clock import load_clock
from app.config import settings
from app.db import SessionLocal, init_db
from app.guide import router as guide_router
from app.provider import FakeCloudProvider

STORY_DESCRIPTION = """
**Start here first:** [`/guide`](/guide) is a narrated, single-page walkthrough of this whole
service -- rules, architecture diagrams, and "try it" boxes that call the real endpoints below
and show you the real response, no terminal required. This Swagger page is the detailed
reference for *after* you've read that.
 
Welcome to the **Shared GPU Job Broker** -- a service that accepts jobs from teams, places
them on simulated cloud nodes, starts and stops those nodes to control cost, and reports
spend per team.
 
Nothing in this service runs in real time: there's a **simulated clock** that only moves
forward when you explicitly tell it to (see `POST /simulator/advance`). This means you can
watch a node take 5 simulated minutes to boot, or a whole week of activity play out, just as
fast as you click "Execute" below.
 
### Follow the story in order
 
1. **Submit a job** -- `POST /jobs`. It starts out `queued`.
2. **Advance the clock** -- `POST /simulator/advance` with e.g. `{"minutes": 10}`. This is
   where everything actually happens: a node gets launched, boots up, and your job starts
   running. Check `GET /nodes` and `GET /jobs/{id}` in between advances to watch the states
   change.
3. **Inspect / cancel** -- `GET /jobs/{id}` shows the full history; `POST /jobs/{id}/cancel`
   stops it early; `POST /jobs/{id}/complete` ends a running job manually if you don't want to
   wait out its full duration.
4. **Check the bill** -- `GET /costs` breaks down GPU-hours and dollars per team, separating
   out idle time and platform overhead (nodes that never got to run anyone's job).
5. **Try the Retrieval/AI track** -- `POST /retrieval/search` with a failed job's log, or just
   a plain-English question, to find similar past failures and how they were fixed.
 
See `docs/DESIGN.md` in the repository for the reasoning behind every policy choice (fairness,
interruptible nodes, idle-cost attribution, and the trace-replay policy comparison), and
`README.md` for how to run the test suite and the trace replay from the command line.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    session = SessionLocal()
    try:
        # Naive UTC on purpose: the clock and DB columns are timezone-naive.
        default_start = datetime.now(UTC).replace(tzinfo=None)
        app.state.clock = load_clock(session, default_start=default_start)
    finally:
        session.close()
    app.state.provider = FakeCloudProvider(rng=random.Random(settings.random_seed))
    yield


app = FastAPI(
    title="GPU Job Broker",
    description=STORY_DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(guide_router)
app.include_router(jobs.router)
app.include_router(nodes.router)
app.include_router(simulator.router)
app.include_router(costs.router)
app.include_router(retrieval.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The guide is the intended front door -- see app/guide.py."""
    return RedirectResponse(url="/guide")
