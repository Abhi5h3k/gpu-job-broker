"""Requirement 7: "After a restart, simulated time and in-flight jobs
resume correctly." Tested at the real persistence layer -- a file-backed
SQLite database, written to by one Broker instance, then read back by a
completely fresh one (a new engine, session, clock, and provider), the
same way a real process restart would work.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.broker import Broker
from app.clock import load_clock, save_clock
from app.config import SchedulingPolicy
from app.models import Base, JobState, Priority
from tests.conftest import START, fast_happy_provider


def open_session(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)()


def test_in_flight_job_and_clock_survive_a_restart(tmp_path):
    db_path = tmp_path / "restart.db"

    # "Process 1": submit a job, let it start running, then stop -- as if
    # the process was killed mid-job.
    session1 = open_session(db_path)
    clock1 = load_clock(session1, default_start=START)
    broker1 = Broker(session1, clock1, fast_happy_provider(), policy=SchedulingPolicy.BALANCED)
    job = broker1.submit_job(team="vision", gpus=1, estimated_minutes=20, priority=Priority.NORMAL)
    broker1.advance(10)  # booted and now running, but not finished
    mid_flight = broker1.get_job(job.id)
    assert mid_flight.state == JobState.RUNNING
    time_at_shutdown = clock1.now
    session1.close()

    # "Process 2": brand-new engine, session, clock, and provider, all
    # pointed at the same file. Nothing from process 1 is reused in memory.
    session2 = open_session(db_path)
    clock2 = load_clock(session2, default_start=START)
    assert clock2.now == time_at_shutdown, (
        "the simulated clock must resume exactly where it left off"
    )

    broker2 = Broker(session2, clock2, fast_happy_provider(), policy=SchedulingPolicy.BALANCED)
    resumed_job = broker2.get_job(job.id)
    assert resumed_job.state == JobState.RUNNING, (
        "the in-flight job must still be there and still running"
    )
    assert resumed_job.id == job.id

    # It should keep going and finish normally after the restart.
    broker2.advance(15)
    assert broker2.get_job(job.id).state == JobState.SUCCEEDED
    save_clock(session2, clock2)
    session2.close()
