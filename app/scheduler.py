"""Scheduling policy: which queued job gets placed next, which node it
goes on, and when a node is considered idle long enough to reap.

This module is deliberately kept free of database *writes* beyond the
straightforward state changes placement itself requires (job/node fields)
-- it doesn't talk to the clock or the provider directly except through
plain parameters, so :func:`order_queue` and :func:`pick_node_type_for`
can be unit-tested with plain in-memory objects, no app startup required.
The stateful orchestration (calling the provider, logging job events,
updating billing segments) lives in broker.py, which calls into this
module for the actual decisions.

See docs/DESIGN.md for the full justification of the fairness policy and
the two scheduling policies compared in the trace replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import SchedulingPolicy
from app.models import Job, JobState, Node, NodeState, NodeType, Priority

# ---- Fairness / queue ordering ---------------------------------------------

_PRIORITY_RANK = {Priority.LOW: 0.0, Priority.NORMAL: 1.0, Priority.HIGH: 2.0}

# How fast a waiting job "ages" into a higher effective priority tier.
# Chosen so a low-priority job ages into normal-priority territory after
# about 2 hours of waiting, and into high-priority territory after about 4
# hours -- long enough that priority ordering is respected most of the
# time, short enough that nothing waits forever. This is the aging half of
# the fairness policy described in docs/DESIGN.md.
AGING_LEVELS_PER_HOUR = 0.5


def effective_priority_score(job: Job, now: datetime) -> float:
    """A job's priority, boosted the longer it has waited. Used only to
    *order* the queue -- it's never persisted."""
    waited_hours = max((now - job.submitted_at).total_seconds() / 3600.0, 0.0)
    return _PRIORITY_RANK[Priority(job.priority)] + waited_hours * AGING_LEVELS_PER_HOUR


def team_gpu_minutes_consumed(session: Session, team: str) -> float:
    """Lifetime total GPU-minutes a team has actually consumed so far
    (sum of gpus_requested * total_run_minutes_accrued over all of its
    jobs). Used only as a fairness tiebreaker, not a hard limit.

    We use a lifetime total rather than a decaying recent-usage window for
    simplicity -- see docs/DESIGN.md's fairness section for why that's a
    reasonable simplification here, and what a natural refinement would be.
    """
    jobs = session.query(Job).filter(Job.team == team).all()
    return sum(j.gpus_requested * j.total_run_minutes_accrued for j in jobs)


def order_queue(
    session: Session, queued_jobs: list[Job], now: datetime, policy: SchedulingPolicy
) -> list[Job]:
    """Decide the order in which queued jobs should be offered a slot.

    Balanced: priority+aging first, then prefer the team that has consumed
    the least GPU-time so far (max-min-ish fairness), then first-submitted.

    Latency-first: plain priority, then first-submitted -- no aging, no
    per-team fairness term. This is the doc's ML-engineer persona taken
    literally: ordering is simple and speed-focused, not cost/fairness
    aware. See the "Replay & policy comparison" section of docs/DESIGN.md.
    """
    if policy == SchedulingPolicy.LATENCY_FIRST:
        return sorted(
            queued_jobs,
            key=lambda j: (-_PRIORITY_RANK[Priority(j.priority)], j.submitted_at),
        )

    usage_cache: dict[str, float] = {}

    def team_usage(team: str) -> float:
        if team not in usage_cache:
            usage_cache[team] = team_gpu_minutes_consumed(session, team)
        return usage_cache[team]

    return sorted(
        queued_jobs,
        key=lambda j: (
            -effective_priority_score(j, now),
            team_usage(j.team),
            j.submitted_at,
        ),
    )


# ---- Node selection ----------------------------------------------------------

#: Smallest-to-largest node types, used to pick the cheapest node that fits
#: a job's GPU request (bin packing for new launches).
_NODE_TYPES_BY_SIZE: list[tuple[NodeType, int]] = [
    (NodeType.GPU_S, 1),
    (NodeType.GPU_M, 4),
    (NodeType.GPU_L, 8),
]


#: The biggest single node we can ever launch -- a job asking for more
#: GPUs than this can never be placed, no matter how long it waits.
MAX_NODE_GPU_CAPACITY = max(capacity for _, capacity in _NODE_TYPES_BY_SIZE)


def pick_node_type_for(gpus_needed: int) -> NodeType:
    """The cheapest node type that has enough GPUs for this job. Only call
    this once you've checked ``gpus_needed <= MAX_NODE_GPU_CAPACITY``
    (see Broker.submit_job, which rejects oversized jobs up front)."""
    for node_type, capacity in _NODE_TYPES_BY_SIZE:
        if capacity >= gpus_needed:
            return node_type
    raise ValueError(f"No node type large enough for {gpus_needed} GPUs")


def should_use_interruptible(job: Job, policy: SchedulingPolicy) -> bool:
    """Whether a newly-launched node for this job should be interruptible.

    Balanced: yes, *except* for high-priority jobs -- this is the policy's
    answer to "what high priority means beyond ordering" (doc explicitly
    leaves this open): high priority also buys reliable, non-reclaimable
    capacity, not just a place at the front of the queue.

    Latency-first: never -- it avoids any reclaim risk entirely, matching
    the ML-engineer persona's "I don't care about cost."
    """
    if policy == SchedulingPolicy.LATENCY_FIRST:
        return False
    return Priority(job.priority) != Priority.HIGH


def find_node_with_free_gpus(
    session: Session, gpus_needed: int, team: str, isolate_teams_per_node: bool
) -> Node | None:
    """The best existing READY node to place this job on, or None if no
    existing node has room. "Best" = tightest fit (least leftover free
    GPUs), to pack jobs densely and avoid launching nodes unnecessarily.
    """
    candidates = (
        session.query(Node)
        .filter(Node.state == NodeState.READY, Node.gpus_free >= gpus_needed)
        .all()
    )
    if isolate_teams_per_node:
        candidates = [n for n in candidates if n.owner_team in (None, team)]
    if not candidates:
        return None
    return min(candidates, key=lambda n: n.gpus_free)


def active_team_gpu_shares(session: Session, node: Node) -> dict[str, float]:
    """Which teams currently have jobs running on this node, and what
    fraction of its total GPUs each of them occupies -- the input billing
    needs to attribute this node's cost correctly (see billing.py)."""
    running_jobs = (
        session.query(Job)
        .filter(Job.node_id == node.id, Job.state == JobState.RUNNING)
        .all()
    )
    shares: dict[str, float] = {}
    for job in running_jobs:
        shares[job.team] = (
            shares.get(job.team, 0.0) + job.gpus_requested / node.gpus_total
        )
    return shares


# ---- Idle reaper --------------------------------------------------------------


@dataclass
class ReapCandidate:
    node: Node


def find_idle_nodes_to_reap(
    session: Session, now: datetime, idle_reaper_minutes: float
) -> list[Node]:
    """Nodes with zero running jobs for longer than the configured idle
    window. Deliberately excludes nodes already mid-teardown
    (RECLAIMING/TERMINATED/FAILED) and nodes still booting (they haven't
    had a chance to run anything yet, and are already being paid for
    regardless -- reaping them wouldn't change what's billed, only when
    the number we were already going to pay is realized)."""
    candidates = (
        session.query(Node)
        .filter(Node.state == NodeState.READY, Node.gpus_free == Node.gpus_total)
        .all()
    )
    due = []
    for node in candidates:
        if node.empty_since is None:
            continue
        idle_minutes = (now - node.empty_since).total_seconds() / 60.0
        if idle_minutes >= idle_reaper_minutes:
            due.append(node)
    return due
