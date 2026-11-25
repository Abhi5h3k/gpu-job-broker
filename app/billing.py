"""Cost tracking: node pricing, the per-node 10-minute billing floor, and
attributing every billed second to a team (or to ``platform_overhead``).

Design summary (see docs/DESIGN.md for the full reasoning):

- A node's lifetime is a sequence of non-overlapping :class:`UsageSegment`
  rows. The broker calls :func:`recompute_segments_for_node` every time the
  set of jobs running on a node changes (a job starts, stops, or the node
  is still booting/idle); this function closes whatever segment(s) were
  open and opens the correct new one(s).
- **Boot time is always ``platform_overhead``.** From ``launched_at``
  until the node's first job actually starts running, nobody has anything
  running yet -- this is pure infrastructure startup cost, independent of
  whether the node then goes on to serve its intended job successfully or
  turns out to be one of the ~3% that never becomes ready. Treating both
  cases the same way is simpler than retroactively reclassifying a boot
  segment only on failure, and it draws an honest, simple line: a team is
  billed once a node is actually theirs to use, not for how long
  infrastructure took to show up (or failed to).
- **Idle time (a node with zero running jobs, after having run at least
  one job) is attributed to the team(s) that most recently ran on it**,
  split in the same proportions they held in that last running segment.
  This directly answers the doc's "idle node time costs money too...
  decide how to attribute it and say why."
- **The 10-minute-per-node billing floor** is applied once, at node
  teardown, by stretching the node's final segment so the sum of all of
  its segments' durations equals ``max(actual_elapsed, 10 minutes)`` --
  whatever that final segment's attribution already was (team, idle, or
  overhead) is what absorbs the extra billed time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import INTERRUPTIBLE_PRICE_FACTOR, MINIMUM_BILLED_SECONDS, NODE_CATALOG
from app.models import (
    PLATFORM_OVERHEAD_TEAM,
    JobEvent,
    JobState,
    Node,
    SegmentType,
    UsageSegment,
)


def node_price_per_hour(node: Node) -> float:
    """The provider's hourly rate for this node, discounted for
    interruptible nodes (doc: "35% of the price")."""
    base = NODE_CATALOG[node.node_type].price_per_hour
    return base * INTERRUPTIBLE_PRICE_FACTOR if node.is_interruptible else base


def node_price_per_second(node: Node) -> float:
    return node_price_per_hour(node) / 3600.0


# ---- Usage segment bookkeeping --------------------------------------------


def _open_segment(
    session: Session,
    node: Node,
    team: str,
    segment_type: SegmentType,
    start_at: datetime,
    gpu_share: float,
) -> UsageSegment:
    seg = UsageSegment(
        node_id=node.id,
        team=team,
        segment_type=segment_type,
        start_at=start_at,
        gpu_share=gpu_share,
    )
    session.add(seg)
    return seg


def _open_segments_for_node(session: Session, node: Node) -> list[UsageSegment]:
    return (
        session.query(UsageSegment)
        .filter(UsageSegment.node_id == node.id, UsageSegment.end_at.is_(None))
        .all()
    )


def recompute_segments_for_node(
    session: Session,
    node: Node,
    now: datetime,
    active_team_gpu_shares: dict[str, float],
) -> None:
    """Close whatever usage segment(s) are currently open for ``node`` and
    open the correct new one(s), given the current set of running jobs.

    ``active_team_gpu_shares`` maps team name -> fraction of the node's
    GPUs that team currently occupies (0 < share <= 1, summing to <= 1
    across teams). An empty dict means the node currently has zero running
    jobs (idle, or still booting).

    Called by the broker every time a job starts, stops, or is requeued
    off this node -- i.e. every time the active set could have changed.
    """
    previously_open = _open_segments_for_node(session, node)
    # Remember what the *running* segments looked like before closing them,
    # so an idle segment opened right after can inherit the same
    # attribution (the "last team(s) to run on it" rule).
    last_running_shares = {
        seg.team: seg.gpu_share
        for seg in previously_open
        if seg.segment_type == SegmentType.RUNNING
    }
    for seg in previously_open:
        seg.end_at = now

    if active_team_gpu_shares:
        node.ever_ran_a_job = True
        node.empty_since = None
        for team, share in active_team_gpu_shares.items():
            _open_segment(session, node, team, SegmentType.RUNNING, now, share)
        return

    node.empty_since = node.empty_since or now

    if not node.ever_ran_a_job:
        # Still booting (or booted but no job placed on it yet) and it has
        # never run anything -- pure platform overhead, not any team's cost.
        _open_segment(
            session, node, PLATFORM_OVERHEAD_TEAM, SegmentType.OVERHEAD, now, 1.0
        )
        return

    # Idle after having run something: attribute to whoever ran last,
    # in the same proportions they held.
    shares = last_running_shares or {PLATFORM_OVERHEAD_TEAM: 1.0}
    for team, share in shares.items():
        _open_segment(session, node, team, SegmentType.IDLE, now, share)


def finalize_node_billing(
    session: Session, node: Node, terminated_at: datetime
) -> None:
    """Close out a node's billing for good: close any still-open segments,
    then apply the 10-minute-per-node minimum by stretching the final
    segment if the node's real lifetime was shorter than that.
    """
    for seg in _open_segments_for_node(session, node):
        seg.end_at = terminated_at

    segments = (
        session.query(UsageSegment)
        .filter(UsageSegment.node_id == node.id)
        .order_by(UsageSegment.start_at)
        .all()
    )
    if not segments:
        # A node that was terminated the instant it was created (shouldn't
        # normally happen, but be defensive): bill the floor as overhead.
        seg = _open_segment(
            session,
            node,
            PLATFORM_OVERHEAD_TEAM,
            SegmentType.OVERHEAD,
            node.launched_at,
            1.0,
        )
        seg.end_at = node.launched_at + timedelta(seconds=MINIMUM_BILLED_SECONDS)
        return

    actual_elapsed = (terminated_at - node.launched_at).total_seconds()
    if actual_elapsed < MINIMUM_BILLED_SECONDS:
        shortfall = MINIMUM_BILLED_SECONDS - actual_elapsed
        last_segment = segments[-1]
        last_segment.end_at = (last_segment.end_at or terminated_at) + timedelta(
            seconds=shortfall
        )


# ---- Cost reporting ---------------------------------------------------------


@dataclass
class TeamCost:
    team: str
    gpu_hours: float = 0.0
    running_cost: float = 0.0
    idle_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.running_cost + self.idle_cost


@dataclass
class CostReport:
    window_start: datetime
    window_end: datetime
    teams: dict[str, TeamCost] = field(default_factory=dict)
    platform_overhead_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return (
            sum(t.total_cost for t in self.teams.values()) + self.platform_overhead_cost
        )


def _overlap_seconds(
    seg_start: datetime, seg_end: datetime, window_start: datetime, window_end: datetime
) -> float:
    start = max(seg_start, window_start)
    end = min(seg_end, window_end)
    return max((end - start).total_seconds(), 0.0)


def _job_running_intervals(
    session: Session, job_id: str, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Reconstruct the time intervals a job actually spent RUNNING, from
    its JobEvent audit trail. A job can have more than one running
    interval if it was interrupted and requeued (see docs/DESIGN.md).
    """
    events = (
        session.query(JobEvent)
        .filter(JobEvent.job_id == job_id)
        .order_by(JobEvent.at)
        .all()
    )
    intervals: list[tuple[datetime, datetime]] = []
    running_since: datetime | None = None
    for event in events:
        if event.state == JobState.RUNNING:
            running_since = event.at
        elif running_since is not None:
            intervals.append((running_since, event.at))
            running_since = None
    if running_since is not None:
        intervals.append((running_since, now))
    return intervals


def compute_cost_report(
    session: Session,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    team: str | None = None,
) -> CostReport:
    """GPU-hours and $ cost per team for ``[window_start, window_end)``.

    GPU-hours uses the doc's definition literally ("the GPUs a job
    requested multiplied by the hours it ran"), reconstructed from each
    job's actual running intervals so it's correct for interrupted jobs
    and for querying an arbitrary sub-window of a longer run.

    $ cost comes from the usage-segment ledger (see module docstring),
    clipped to the window, which is how idle time and platform overhead
    get reported alongside the "useful" running cost.
    """
    report = CostReport(window_start=window_start, window_end=window_end)

    nodes_by_id = {n.id: n for n in session.query(Node).all()}
    segments = (
        session.query(UsageSegment).filter(UsageSegment.start_at < window_end).all()
    )
    for seg in segments:
        if team is not None and seg.team != team:
            continue
        node = nodes_by_id.get(seg.node_id)
        if node is None:
            continue
        seg_end = seg.end_at or now
        seconds = _overlap_seconds(seg.start_at, seg_end, window_start, window_end)
        if seconds <= 0:
            continue
        cost = node_price_per_second(node) * seconds * seg.gpu_share

        if seg.team == PLATFORM_OVERHEAD_TEAM:
            report.platform_overhead_cost += cost
            continue

        bucket = report.teams.setdefault(seg.team, TeamCost(team=seg.team))
        if seg.segment_type == SegmentType.RUNNING:
            bucket.running_cost += cost
        else:
            bucket.idle_cost += cost

    jobs_query = session.query(JobEvent.job_id).distinct()
    job_ids = [row[0] for row in jobs_query.all()]
    from app.models import Job  # local import avoids a module-level cycle

    jobs_by_id = {j.id: j for j in session.query(Job).filter(Job.id.in_(job_ids)).all()}
    for job_id in job_ids:
        job = jobs_by_id.get(job_id)
        if job is None:
            continue
        if team is not None and job.team != team:
            continue
        intervals = _job_running_intervals(session, job_id, now)
        running_hours = 0.0
        for start, end in intervals:
            running_hours += (
                _overlap_seconds(start, end, window_start, window_end) / 3600.0
            )
        if running_hours <= 0:
            continue
        bucket = report.teams.setdefault(job.team, TeamCost(team=job.team))
        bucket.gpu_hours += job.gpus_requested * running_hours

    return report
