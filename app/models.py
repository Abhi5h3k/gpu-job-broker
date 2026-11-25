"""The database schema: Job, Node, UsageSegment, JobEvent, ClockState.

Design notes (the "why", not just the "what"):

- All timestamps are naive ``datetime`` objects interpreted as UTC by
  convention (the doc states "all times UTC unless stated"). We don't
  store timezone info because there is only ever one timezone in this
  system -- one less thing to get wrong in comparisons.
- Idempotency (requirement 1, "submitting the same client-supplied key
  twice must not create two jobs") is a plain unique constraint on
  ``Job.client_key``, not a separate table. A dedicated idempotency-key
  table would be the "textbook" answer, but here the key and the job are
  created atomically in the same request, so one column does the job.
- Cost attribution (requirement 6) is tracked incrementally as
  ``UsageSegment`` rows rather than computed after the fact from job
  history. Each segment is a contiguous slice of one node's billed time,
  tagged with who it's attributed to (a team, or ``"platform_overhead"``
  for wasted capacity no team caused -- see docs/DESIGN.md). The broker
  closes the current segment and opens a new one every time the set of
  jobs running on a node changes. This keeps cost-report queries a simple
  sum over rows instead of a replay of the whole job history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def new_id(prefix: str) -> str:
    """Short, readable, unique IDs like ``job-3f9a2b1c`` instead of a bare
    UUID -- easier to read in Swagger responses and logs."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# States a job can still be acted on (cancel) or transitioned out of.
ACTIVE_JOB_STATES = (JobState.QUEUED, JobState.RUNNING)
TERMINAL_JOB_STATES = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class JobSource(str, Enum):
    """Where a job came from -- affects how its "true" duration is known.
    See docs/DESIGN.md's job-completion-semantics decision."""

    LIVE = "live"
    REPLAY = "replay"


class NodeType(str, Enum):
    GPU_S = "gpu-s"
    GPU_M = "gpu-m"
    GPU_L = "gpu-l"


class NodeState(str, Enum):
    BOOTING = "booting"
    READY = "ready"
    RECLAIMING = "reclaiming"
    TERMINATED = "terminated"
    FAILED = "failed"  # never became ready; see provider.py


class SegmentType(str, Enum):
    RUNNING = "running"
    IDLE = "idle"
    OVERHEAD = "overhead"


# Pseudo-team name used for platform_overhead segments (wasted capacity no
# team caused, e.g. a node that never became ready).
PLATFORM_OVERHEAD_TEAM = "platform_overhead"


class ClockState(Base):
    """A single-row table holding the broker's current simulated time.

    There is intentionally only ever one row (id=1). Reading/writing this
    row is how the simulated clock survives a restart (requirement 7):
    "After a restart, simulated time resumes where it stopped."
    """

    __tablename__ = "clock_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    now: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # The last time Broker._process_heartbeat actually fired. Persisted
    # for the same reason `now` is: a Broker is reconstructed fresh on
    # every API request (see broker.py's docstring), so without this, the
    # heartbeat anchor would reset to "now" on every request and the
    # periodic reclaim-notice/capacity-retry check could fail to fire for
    # calls that advance the clock by less than HEARTBEAT_INTERVAL_MINUTES
    # at a time. Nullable so a brand-new database (or a test that never
    # loads this row) just falls back to the caller-supplied default.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Whether the most recent scheduling pass hit a true
    # CapacityUnavailableError (as opposed to a job merely waiting behind
    # the max_nodes cap, which needs no flag -- an already-scheduled node
    # event retries it naturally). Persisted for the same per-request
    # reconstruction reason as last_heartbeat_at: without this, a fresh
    # Broker per API request would forget a job is stuck on capacity and
    # the heartbeat would never schedule a retry for it.
    capacity_blocked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: new_id("job")
    )
    client_key: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True, index=True
    )

    team: Mapped[str] = mapped_column(String, nullable=False, index=True)
    gpus_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[Priority] = mapped_column(
        String, nullable=False, default=Priority.NORMAL
    )
    estimated_minutes: Mapped[float] = mapped_column(Float, nullable=False)

    # Only populated for replay jobs -- the ground-truth duration from the
    # trace. Never read by the scheduler (it only ever sees the estimate);
    # only used to seed remaining_run_minutes. Kept for debugging/audit.
    actual_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[JobSource] = mapped_column(
        String, nullable=False, default=JobSource.LIVE
    )

    state: Mapped[JobState] = mapped_column(
        String, nullable=False, default=JobState.QUEUED
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Counts down only while state == RUNNING. Frozen (not reset) across an
    # interruption/requeue -- see docs/DESIGN.md.
    remaining_run_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    # Sum of all running time actually accrued across every run segment
    # (survives interruptions). This is what "GPU-hours ... multiplied by
    # the hours it ran" is computed from, not wall-clock queued-to-done time.
    total_run_minutes_accrued: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    node_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("nodes.id"), nullable=True, index=True
    )

    # Only set for replay jobs whose trace row recorded outcome="failed" or
    # "cancelled" -- the clarification says these rows "count for cost and
    # wait time" at their actual_minutes, so we still let them run for that
    # long, but finish with the trace's recorded outcome instead of always
    # assuming success. None (the default) means "succeed when the clock
    # driven countdown reaches zero," which is every live job's behavior.
    replay_outcome: Mapped[JobState | None] = mapped_column(String, nullable=True)

    # How many times this job has aged in the queue / been requeued after
    # an interruption -- surfaced in the API purely for transparency about
    # the fairness policy; not used for anything else.
    requeue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class JobEvent(Base):
    """Append-only state-transition log for a job, so ``GET /jobs/{id}``
    can show its full history, not just the current state."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("jobs.id"), nullable=False, index=True
    )
    state: Mapped[JobState] = mapped_column(String, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: new_id("node")
    )
    node_type: Mapped[NodeType] = mapped_column(String, nullable=False)
    is_interruptible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    gpus_total: Mapped[int] = mapped_column(Integer, nullable=False)
    gpus_free: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[NodeState] = mapped_column(
        String, nullable=False, default=NodeState.BOOTING
    )
    launched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Internal "dice roll" results, decided at launch time -- never exposed
    # directly via the API, just drives provider.py's event scheduling.
    doomed_never_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    never_ready_deadline: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # Interruptible-reclaim bookkeeping (unused for on-demand nodes).
    reclaim_notice_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reclaim_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Set the instant the node has zero running jobs; cleared the instant a
    # job starts running on it again. The idle reaper compares this against
    # now() - idle_reaper_minutes. Also doubles as "never had a job" marker
    # when equal to ready_at and no job ever ran (see billing.py overhead
    # handling).
    empty_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ever_ran_a_job: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Only meaningful when isolate_teams_per_node=True: the one team
    # allowed to use this node.
    owner_team: Mapped[str | None] = mapped_column(String, nullable=True)


class UsageSegment(Base):
    """One contiguous, billable slice of a node's lifetime, attributed to
    a team (or PLATFORM_OVERHEAD_TEAM). See the module docstring for why
    this table exists instead of computing cost after the fact."""

    __tablename__ = "usage_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(
        String, ForeignKey("nodes.id"), nullable=False, index=True
    )
    team: Mapped[str] = mapped_column(String, nullable=False, index=True)
    segment_type: Mapped[SegmentType] = mapped_column(String, nullable=False)

    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Null while the segment is still open (the node is still in this state).
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Fraction of the node's hourly price attributed to this segment, e.g.
    # 0.5 if this job only occupies half the node's GPUs while running
    # alongside another team's job. 1.0 for idle/overhead segments (the
    # whole node is "spent" either way).
    gpu_share: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
