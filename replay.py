"""Replay a week's worth of job submissions through the exact same broker
code the live API uses, and compare two scheduling policies (requirement
8): "Balanced" (the default -- priority + anti-starvation aging + a
fairness tiebreaker, using interruptible nodes for non-urgent work) versus
"Latency-first" (always launch a dedicated on-demand node immediately,
plain priority + FIFO ordering -- what an ML engineer who only cares about
their own job's start time would ask for).

Each policy run gets its own fresh in-memory database and its own
SimulatedClock starting at the trace's first submission -- the same clock
and broker classes the live API uses, just driven by the CSV instead of
HTTP requests, so this is a genuine apples-to-apples test of the
scheduling logic.

Usage:
    uv run python replay.py data/01a_trace.csv
"""

from __future__ import annotations

import csv
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.billing import compute_cost_report
from app.broker import Broker
from app.clock import SimulatedClock
from app.config import SchedulingPolicy
from app.models import Base, Job, JobSource, JobState, Priority
from app.provider import FakeCloudProvider

# After the last trace row is submitted, keep advancing the clock by this
# much so every still-running or still-queued job has a chance to finish --
# generous relative to the longest job in the trace (the data's longest
# actual_minutes is 2 days; this leaves plenty of room for queue delay too).
DRAIN_BUFFER = timedelta(days=4)

REPLAY_SEED = 1337


@dataclass
class TraceRow:
    submitted_at: datetime
    team: str
    gpus: int
    estimated_minutes: float
    actual_minutes: float
    priority: Priority
    outcome: JobState


def _parse_submitted_at(raw: str) -> datetime:
    """A few trace rows use a non-UTC offset (e.g. +01:00) instead of 'Z' --
    normalize everything to naive UTC so it's directly comparable with the
    rest of the app's (naive, always-UTC) datetimes."""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def load_trace(path: Path) -> list[TraceRow]:
    """Parse the trace CSV into TraceRow objects, sorted by submission time
    (the file is expected to already be sorted, but we don't rely on it)."""
    rows: list[TraceRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            rows.append(
                TraceRow(
                    submitted_at=_parse_submitted_at(raw["submitted_at"]),
                    team=raw["team"],
                    gpus=int(raw["gpus"]),
                    estimated_minutes=float(raw["estimated_minutes"]),
                    actual_minutes=float(raw["actual_minutes"]),
                    priority=Priority(raw["priority"]),
                    outcome=JobState(raw["outcome"]),
                )
            )
    rows.sort(key=lambda r: r.submitted_at)
    return rows


def run_policy(
    rows: list[TraceRow], policy: SchedulingPolicy
) -> tuple[Session, Broker, list[str]]:
    """Feed every trace row into a fresh broker, in submission order,
    advancing the clock to each row's timestamp before submitting it so
    everything in between (node boots, completions, reclaims) plays out
    exactly as it would have at the time."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)()

    clock = SimulatedClock(now=rows[0].submitted_at)
    provider = FakeCloudProvider(rng=random.Random(REPLAY_SEED))
    broker = Broker(session, clock, provider, policy=policy)

    job_ids: list[str] = []
    for row in rows:
        if clock.now < row.submitted_at:
            broker.advance_to(row.submitted_at)
        job = broker.submit_job(
            team=row.team,
            gpus=row.gpus,
            estimated_minutes=row.estimated_minutes,
            priority=row.priority,
            source=JobSource.REPLAY,
            actual_minutes=row.actual_minutes,
            submitted_at=row.submitted_at,
            replay_outcome=row.outcome,
        )
        job_ids.append(job.id)

    broker.advance_to(rows[-1].submitted_at + DRAIN_BUFFER)
    return session, broker, job_ids


def wait_minutes(job: Job) -> float:
    """Queue delay: submission until the job got a node and started
    running. For a job that never started at all (cancelled/failed while
    still queued), we count the whole span until it was resolved -- it was
    "waiting" for the entire time it existed."""
    end = job.started_at or job.completed_at
    if end is None:
        return 0.0
    return (end - job.submitted_at).total_seconds() / 60.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lower, upper = int(rank), min(int(rank) + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def summarize(session: Session, broker: Broker, job_ids: list[str]) -> dict:
    jobs = session.query(Job).filter(Job.id.in_(job_ids)).all()
    waits_by_team: dict[str, list[float]] = {}
    outcomes: dict[str, int] = {}
    for job in jobs:
        waits_by_team.setdefault(job.team, []).append(wait_minutes(job))
        outcomes[job.state] = outcomes.get(job.state, 0) + 1

    report = compute_cost_report(
        session, datetime.min, broker.clock.now, broker.clock.now
    )
    return {
        "wait_by_team": {
            team: {
                "n": len(values),
                "p50": percentile(values, 0.50),
                "p90": percentile(values, 0.90),
                "p99": percentile(values, 0.99),
            }
            for team, values in sorted(waits_by_team.items())
        },
        "outcomes": outcomes,
        "total_cost": report.total_cost,
        "platform_overhead_cost": report.platform_overhead_cost,
        "team_costs": {t: c.total_cost for t, c in report.teams.items()},
    }


def print_summary(policy: SchedulingPolicy, summary: dict) -> None:
    print(f"\n=== Policy: {policy.value} ===")
    print(f"Job outcomes: {summary['outcomes']}")
    print(
        f"Total cost: ${summary['total_cost']:.2f}  (platform overhead: ${summary['platform_overhead_cost']:.2f})"
    )
    print("\nWait-time percentiles per team (minutes, submission -> started running):")
    print(
        f"  {'team':12s} {'n':>5s} {'p50':>8s} {'p90':>8s} {'p99':>8s} {'$ cost':>10s}"
    )
    for team, stats in summary["wait_by_team"].items():
        cost = summary["team_costs"].get(team, 0.0)
        print(
            f"  {team:12s} {stats['n']:5d} {stats['p50']:8.1f} {stats['p90']:8.1f} "
            f"{stats['p99']:8.1f} {cost:10.2f}"
        )


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/01a_trace.csv")
    rows = load_trace(path)
    print(f"Loaded {len(rows)} trace rows from {path}")

    for policy in (SchedulingPolicy.BALANCED, SchedulingPolicy.LATENCY_FIRST):
        session, broker, job_ids = run_policy(rows, policy)
        print_summary(policy, summarize(session, broker, job_ids))
        session.close()

    print(
        "\nSee docs/DESIGN.md for what these two policies mean and why "
        "'Balanced' is the recommended default."
    )


if __name__ == "__main__":
    main()
