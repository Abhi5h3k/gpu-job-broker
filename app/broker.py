"""The orchestrator: the one place that ties the clock, the fake provider,
the scheduler, and billing together into the job/node lifecycle state
machine described in the doc.

Everything here is a plain, synchronous, in-process method call -- there
is no background task, no queue, no async work. A :class:`Broker` is
cheap to construct (it just wraps a DB session, a clock, and a provider)
and is created fresh per API request (see app/main.py) or once for the
whole duration of a trace replay (see replay.py at the repo root).

The two things every other module eventually calls into are:

- :meth:`Broker.submit_job` / :meth:`Broker.cancel_job` /
  :meth:`Broker.complete_job` -- the "a call to your service" side of job
  lifecycle management.
- :meth:`Broker.advance_to` / :meth:`Broker.advance` -- the "simulated
  clock" side: moves time forward and processes every due event
  (node-ready, node-failed, reclaim, job-completion, idle-reaper sweep,
  and a retry of any queued job still waiting on capacity) in order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import billing, scheduler
from app.clock import (
    SimulatedClock,
    load_capacity_blocked,
    load_heartbeat_anchor,
    save_capacity_blocked,
    save_clock,
    save_heartbeat_anchor,
)
from app.config import NODE_CATALOG, SchedulingPolicy, settings
from app.models import (
    ACTIVE_JOB_STATES,
    Job,
    JobEvent,
    JobSource,
    JobState,
    Node,
    NodeState,
    Priority,
)
from app.provider import CapacityUnavailableError, FakeCloudProvider

# How often (in simulated minutes) the broker re-checks interruptible
# nodes for a reclaim notice, and retries launches that are still stuck on
# CapacityUnavailable. See docs/DESIGN.md for why this cadence, not a
# continuous check, is both sufficient and what keeps advance() fast.
HEARTBEAT_INTERVAL_MINUTES = 1.0


class JobNotFoundError(Exception):
    pass


class NodeNotFoundError(Exception):
    pass


class InvalidJobStateError(Exception):
    """Raised when an operation doesn't make sense for a job's current
    state, e.g. cancelling an already-succeeded job."""


@dataclass
class AdvanceSummary:
    """What happened during one ``advance()`` call -- returned to the
    caller (the Swagger /simulator/advance endpoint, or the replay driver)
    so it's visible what the clock jump actually did, not just that time
    moved."""

    nodes_ready: list[str] = field(default_factory=list)
    nodes_failed_never_ready: list[str] = field(default_factory=list)
    nodes_reclaimed: list[str] = field(default_factory=list)
    nodes_reaped_idle: list[str] = field(default_factory=list)
    jobs_completed: list[str] = field(default_factory=list)
    jobs_requeued_after_reclaim: list[str] = field(default_factory=list)
    capacity_unavailable_events: int = 0


class Broker:
    def __init__(
        self,
        session: Session,
        clock: SimulatedClock,
        provider: FakeCloudProvider,
        *,
        policy: SchedulingPolicy | None = None,
        isolate_teams_per_node: bool | None = None,
        idle_reaper_minutes: float | None = None,
        max_nodes: int | None = None,
    ) -> None:
        self.session = session
        self.clock = clock
        self.provider = provider
        # Every tunable defaults to the global settings, but can be
        # overridden per-instance -- this is what lets replay.py run the
        # exact same broker code under two different policies for the
        # requirement-8 comparison without touching global config.
        self.policy = policy or settings.scheduling_policy
        self.isolate_teams_per_node = (
            settings.isolate_teams_per_node
            if isolate_teams_per_node is None
            else isolate_teams_per_node
        )
        self.idle_reaper_minutes = (
            settings.idle_reaper_minutes
            if idle_reaper_minutes is None
            else idle_reaper_minutes
        )
        self.max_nodes = settings.max_nodes if max_nodes is None else max_nodes
        # Loaded from the DB (falling back to clock.now for a brand-new
        # database or a test session that never persisted one) rather than
        # always defaulting to clock.now -- see load_heartbeat_anchor's
        # docstring: a Broker is reconstructed fresh per API request, so
        # defaulting here unconditionally would reset the heartbeat cadence
        # on every single request.
        self._last_heartbeat_at = load_heartbeat_anchor(session, default=clock.now)
        self._capacity_unavailable_count = 0
        # True only while the most recent launch attempt was turned away by
        # the provider's bursty CapacityUnavailableError -- the one case
        # where nothing else (a node finishing boot, a job completing, a
        # node being reaped) will naturally wake the scheduler back up, so
        # the heartbeat needs to keep polling until capacity returns. A job
        # merely waiting its turn behind the max_nodes cap or an existing
        # node doesn't need this: a job-completion/node-teardown event is
        # already scheduled and will retry it at the right time.
        #
        # Loaded from the DB rather than hardcoded False, for the same
        # per-request-reconstruction reason as _last_heartbeat_at above --
        # see load_capacity_blocked's docstring.
        self._capacity_blocked = load_capacity_blocked(session, default=False)

    # ---- Job lifecycle -----------------------------------------------------

    def submit_job(
        self,
        *,
        team: str,
        gpus: int,
        estimated_minutes: float,
        priority: Priority = Priority.NORMAL,
        client_key: str | None = None,
        source: JobSource = JobSource.LIVE,
        actual_minutes: float | None = None,
        submitted_at: datetime | None = None,
        replay_outcome: JobState | None = None,
    ) -> Job:
        """Create a job, or return the existing one if ``client_key`` was
        already used (requirement 1's idempotency rule)."""
        if client_key:
            existing = (
                self.session.query(Job)
                .filter(Job.client_key == client_key)
                .one_or_none()
            )
            if existing is not None:
                return existing

        now = submitted_at or self.clock.now
        # Ground truth for when this job stops running: the real
        # actual_minutes for a replay job, or the caller's own estimate for
        # a live job -- see docs/DESIGN.md's job-completion-semantics note
        # on why a live job has no better ground truth to use.
        remaining = actual_minutes if source == JobSource.REPLAY else estimated_minutes

        job = Job(
            team=team,
            gpus_requested=gpus,
            priority=priority,
            estimated_minutes=estimated_minutes,
            actual_minutes=actual_minutes,
            source=source,
            state=JobState.QUEUED,
            submitted_at=now,
            remaining_run_minutes=remaining,
            client_key=client_key,
            replay_outcome=replay_outcome,
        )
        self.session.add(job)
        self.session.flush()
        self._log_event(job, JobState.QUEUED, now)

        if gpus > scheduler.MAX_NODE_GPU_CAPACITY:
            # No node we can ever launch is big enough for this request --
            # fail fast instead of leaving it queued forever (or crashing
            # the scheduler). The live API's schema already blocks this for
            # normal clients; this is a defense-in-depth check for replay
            # and any other direct caller.
            job.state = JobState.FAILED
            job.completed_at = now
            self._log_event(
                job,
                JobState.FAILED,
                now,
                note=f"requested {gpus} GPUs, larger than the biggest node ({scheduler.MAX_NODE_GPU_CAPACITY} GPUs)",
            )
        else:
            self._run_scheduling_pass()
        self.session.commit()
        return job

    def get_job(self, job_id: str) -> Job:
        job = self.session.get(Job, job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def cancel_job(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.state not in ACTIVE_JOB_STATES:
            raise InvalidJobStateError(
                f"job {job_id} is already {job.state}, cannot cancel"
            )
        now = self.clock.now
        if job.state == JobState.RUNNING:
            self._accrue_running_time(job, now)
            self._release_job_from_node(job, now)
        job.state = JobState.CANCELLED
        job.completed_at = now
        self._log_event(job, JobState.CANCELLED, now)
        self._run_scheduling_pass()
        self.session.commit()
        return job

    def complete_job(self, job_id: str, outcome: JobState) -> Job:
        """The explicit "a call to your service" completion path -- ends a
        running job early/manually, for either outcome."""
        if outcome not in (JobState.SUCCEEDED, JobState.FAILED):
            raise InvalidJobStateError("outcome must be succeeded or failed")
        job = self.get_job(job_id)
        if job.state != JobState.RUNNING:
            raise InvalidJobStateError(
                f"job {job_id} is {job.state}, not running -- nothing to complete"
            )
        now = self.clock.now
        self._accrue_running_time(job, now)
        job.remaining_run_minutes = 0.0
        self._release_job_from_node(job, now)
        job.state = outcome
        job.completed_at = now
        self._log_event(job, outcome, now)
        self._run_scheduling_pass()
        self.session.commit()
        return job

    def list_queue(self) -> list[Job]:
        queued = self.session.query(Job).filter(Job.state == JobState.QUEUED).all()
        return scheduler.order_queue(self.session, queued, self.clock.now, self.policy)

    def list_nodes(self) -> list[Node]:
        return self.session.query(Node).order_by(Node.launched_at).all()

    # ---- Internal job/node bookkeeping -------------------------------------

    def _log_event(
        self, job: Job, state: JobState, at: datetime, note: str | None = None
    ) -> None:
        self.session.add(JobEvent(job_id=job.id, state=state, at=at, note=note))

    def _accrue_running_time(self, job: Job, now: datetime) -> None:
        """Bank whatever time a running job has spent in its current
        segment into its lifetime total, and reduce what's left -- shared
        by cancel/complete/reclaim-requeue so the counting is identical."""
        if job.started_at is None:
            return
        elapsed_minutes = max((now - job.started_at).total_seconds() / 60.0, 0.0)
        accrued = min(elapsed_minutes, job.remaining_run_minutes)
        job.total_run_minutes_accrued += accrued
        job.remaining_run_minutes = max(job.remaining_run_minutes - accrued, 0.0)

    def _release_job_from_node(self, job: Job, now: datetime) -> None:
        node = self.session.get(Node, job.node_id) if job.node_id else None
        job.node_id = None
        if node is not None:
            node.gpus_free = min(node.gpus_free + job.gpus_requested, node.gpus_total)
            if node.gpus_free >= node.gpus_total:
                # Nobody left running on it -- start the idle-reaper clock
                # (see scheduler.find_idle_nodes_to_reap). Without this, a
                # fully-idle node would never be reaped and would keep
                # occupying a max_nodes slot forever.
                node.empty_since = now
            self._refresh_node_segments(node, now)

    def _refresh_node_segments(self, node: Node, now: datetime) -> None:
        shares = scheduler.active_team_gpu_shares(self.session, node)
        billing.recompute_segments_for_node(self.session, node, now, shares)

    # ---- Scheduling ----------------------------------------------------------

    def _run_scheduling_pass(self) -> None:
        now = self.clock.now
        queued = self.session.query(Job).filter(Job.state == JobState.QUEUED).all()
        if not queued:
            self._capacity_blocked = False
            save_capacity_blocked(self.session, False, now)
            return
        # Recomputed fresh each pass: only true if a launch attempt in
        # *this* pass actually hit CapacityUnavailableError (see
        # _try_launch_node_for and the heartbeat note in _next_event_time).
        self._capacity_blocked = False
        # Computed once per pass and tracked locally (bumped whenever a
        # launch below succeeds) instead of re-querying it for every
        # queued job -- with a large backlog stuck at the cap, that
        # per-job COUNT query was the dominant cost (see docs/DESIGN.md's
        # performance note).
        alive_nodes = (
            self.session.query(Node)
            .filter(
                Node.state.in_(
                    [NodeState.BOOTING, NodeState.READY, NodeState.RECLAIMING]
                )
            )
            .count()
        )
        ordered = scheduler.order_queue(self.session, queued, now, self.policy)
        for job in ordered:
            reserved = self._reserved_ready_node(job)
            if reserved is not None:
                # A node was launched specifically for this job and is now
                # READY -- place it there. This must happen *before* the
                # Latency-first/Balanced branch below: Latency-first never
                # runs the general "share any node with free GPUs" search,
                # so without this check it would have no way to ever use
                # the dedicated node it launched (see docs/DESIGN.md).
                self._place_job_on_node(job, reserved, now)
                continue
            if self._has_pending_launch(job):
                # A node is already booting for this job (see
                # _try_launch_node_for) -- don't launch a second, redundant
                # one every time the scheduler retries. The job stays
                # queued and will be placed once that node is READY.
                continue
            if self.policy != SchedulingPolicy.LATENCY_FIRST:
                node = scheduler.find_node_with_free_gpus(
                    self.session,
                    job.gpus_requested,
                    job.team,
                    self.isolate_teams_per_node,
                )
                if node is not None:
                    self._place_job_on_node(job, node, now)
                    continue
            if alive_nodes >= self.max_nodes:
                continue  # doc: "Maximum 10 nodes alive at once" -- job stays queued
            if self._try_launch_node_for(job, now):
                alive_nodes += 1
        # Persisted (not just kept in memory) so a fresh Broker on the next
        # API request still knows to keep the heartbeat polling -- see
        # load_capacity_blocked's docstring.
        save_capacity_blocked(self.session, self._capacity_blocked, now)

    def _reserved_ready_node(self, job: Job) -> Node | None:
        """If a node was launched specifically for this job (the soft
        reservation set by :meth:`_try_launch_node_for`) and it's now READY
        with enough free capacity, return it. None if there's no
        reservation, it's still booting, or it filled up with other work
        before this job got a turn (e.g. a smaller job jumped in under the
        Balanced policy's sharing) -- a fresh launch attempt is needed in
        that last case too, which falls through to _try_launch_node_for."""
        if job.node_id is None:
            return None
        node = self.session.get(Node, job.node_id)
        if (
            node is not None
            and node.state == NodeState.READY
            and node.gpus_free >= job.gpus_requested
        ):
            return node
        return None

    def _has_pending_launch(self, job: Job) -> bool:
        """True if ``job`` already has a node booting for it specifically
        (set by :meth:`_try_launch_node_for`). Clears a stale reservation
        if that node turned out to be one of the ~3% that never became
        ready, so a fresh launch can be attempted instead."""
        if job.node_id is None:
            return False
        node = self.session.get(Node, job.node_id)
        if node is not None and node.state == NodeState.BOOTING:
            return True
        if node is None or node.state == NodeState.FAILED:
            job.node_id = None
        return False

    def _place_job_on_node(self, job: Job, node: Node, now: datetime) -> None:
        job.node_id = node.id
        job.state = JobState.RUNNING
        job.started_at = now
        node.gpus_free -= job.gpus_requested
        node.empty_since = None  # it's doing work again -- not a reap candidate
        if self.isolate_teams_per_node and node.owner_team is None:
            node.owner_team = job.team
        self._refresh_node_segments(node, now)
        self._log_event(job, JobState.RUNNING, now)

    def _try_launch_node_for(self, job: Job, now: datetime) -> bool:
        """Attempt to launch a fresh node for ``job``. Returns whether a
        node was actually created, so the caller can keep its own running
        count of alive nodes without re-querying it (see the max_nodes
        check in :meth:`_run_scheduling_pass`)."""
        try:
            self.provider.request_capacity(now)
        except CapacityUnavailableError:
            # Doc: "If the provider fails, the system keeps working." The
            # job simply stays queued; the next scheduling pass (triggered
            # by any future event, or the heartbeat) will try again.
            self._capacity_unavailable_count += 1
            self._capacity_blocked = True
            return False
        self._capacity_blocked = False

        node_type = scheduler.pick_node_type_for(job.gpus_requested)
        interruptible = scheduler.should_use_interruptible(job, self.policy)
        spec = NODE_CATALOG[node_type]
        node = Node(
            node_type=node_type,
            is_interruptible=interruptible,
            gpus_total=spec.gpus,
            gpus_free=spec.gpus,
            state=NodeState.BOOTING,
            launched_at=now,
        )
        self.session.add(node)
        self.session.flush()

        plan = self.provider.plan_launch(now)
        node.ready_at = plan.ready_at
        node.doomed_never_ready = plan.doomed_never_ready
        node.never_ready_deadline = plan.never_ready_deadline
        # Billing starts the instant the launch is accepted, regardless of
        # what happens next (see docs/DESIGN.md's billing-boundary note).
        # No job has run on it yet, so this opens as platform_overhead.
        self._refresh_node_segments(node, now)

        # Soft reservation: remember that this specific node is booting for
        # this job, so the next scheduling pass doesn't launch a second,
        # redundant node for the same demand before this one is ready. The
        # job's state stays QUEUED -- this is bookkeeping, not a placement.
        job.node_id = node.id
        return True

    # ---- The simulated clock's event loop -------------------------------------

    def advance(self, minutes: float) -> AdvanceSummary:
        """Move the clock forward by a relative number of minutes."""
        return self.advance_to(self.clock.now + timedelta(minutes=minutes))

    def advance_to(self, target: datetime) -> AdvanceSummary:
        """Move the clock forward to an absolute time, processing every
        due event along the way, in order. Never sleeps -- this is a pure
        loop over database state, so even a very large jump runs in a
        fraction of a real second (see docs/DESIGN.md's clock design note).
        """
        summary = AdvanceSummary()
        self._capacity_unavailable_count = 0
        while True:
            when = self._next_event_time(target)
            if when is None:
                break
            self.clock.set(when)
            self._process_events_at(when, summary)
        self.clock.set(target)
        summary.capacity_unavailable_events = self._capacity_unavailable_count
        save_clock(self.session, self.clock)
        self.session.commit()
        return summary

    def _next_event_time(self, target: datetime) -> datetime | None:
        """The earliest timestamp, strictly after ``now`` and at or before
        ``target``, at which something is due to happen. None if nothing
        is due before ``target`` (including when ``now`` has already
        reached ``target`` -- without this check, a future event beyond
        ``target`` would keep clamping to ``target`` forever and the loop
        in :meth:`advance_to` would never terminate)."""
        if self.clock.now >= target:
            return None

        candidates: list[datetime] = []

        for node in (
            self.session.query(Node)
            .filter(Node.state == NodeState.BOOTING, Node.doomed_never_ready.is_(False))
            .all()
        ):
            if node.ready_at is not None:
                candidates.append(node.ready_at)

        for node in (
            self.session.query(Node)
            .filter(Node.state == NodeState.BOOTING, Node.doomed_never_ready.is_(True))
            .all()
        ):
            if node.never_ready_deadline is not None:
                candidates.append(node.never_ready_deadline)

        for node in (
            self.session.query(Node).filter(Node.state == NodeState.RECLAIMING).all()
        ):
            if node.reclaim_at is not None:
                candidates.append(node.reclaim_at)

        for job in self.session.query(Job).filter(Job.state == JobState.RUNNING).all():
            if job.started_at is not None:
                candidates.append(
                    job.started_at + timedelta(minutes=job.remaining_run_minutes)
                )

        for node in (
            self.session.query(Node)
            .filter(Node.state == NodeState.READY, Node.empty_since.isnot(None))
            .all()
        ):
            candidates.append(
                node.empty_since + timedelta(minutes=self.idle_reaper_minutes)
            )

        # Only bother waking up on a periodic heartbeat if there's
        # something it would actually need to check -- otherwise a huge
        # idle `advance()` call would spin once per simulated minute for no
        # reason. A job merely waiting behind the max_nodes cap or sharing
        # an existing node doesn't count: a job-completion or node-teardown
        # event is already scheduled and will retry it at the right time.
        # See HEARTBEAT_INTERVAL_MINUTES docstring and _capacity_blocked.
        has_interruptible_ready_nodes = (
            self.session.query(Node.id)
            .filter(Node.state == NodeState.READY, Node.is_interruptible.is_(True))
            .first()
            is not None
        )
        if self._capacity_blocked or has_interruptible_ready_nodes:
            candidates.append(
                self._last_heartbeat_at + timedelta(minutes=HEARTBEAT_INTERVAL_MINUTES)
            )

        future = [c for c in candidates if c > self.clock.now]
        if not future:
            return None
        return min(min(future), target)

    def _process_events_at(self, when: datetime, summary: AdvanceSummary) -> None:
        self._process_node_ready(when, summary)
        self._process_node_never_ready(when, summary)
        self._process_reclaim_teardown(when, summary)
        self._process_job_completions(when, summary)
        self._process_heartbeat(when, summary)
        # Scheduling must run *before* the idle reaper: a node that just
        # became READY this instant (or was just freed by a completion)
        # needs a chance to pick up a waiting job first. Otherwise a node
        # whose boot time alone already exceeds idle_reaper_minutes would
        # get torn down the moment it's ready, even with a job waiting for
        # it (a real bug caught by test_billing.py's floor test, which
        # uses a short idle_reaper_minutes to exercise the 10-minute
        # minimum and tripped over this ordering issue).
        self._run_scheduling_pass()
        self._process_idle_reaper(when, summary)

    def _process_node_ready(self, when: datetime, summary: AdvanceSummary) -> None:
        due = (
            self.session.query(Node)
            .filter(
                Node.state == NodeState.BOOTING,
                Node.doomed_never_ready.is_(False),
                Node.ready_at.isnot(None),
                Node.ready_at <= when,
            )
            .all()
        )
        for node in due:
            node.state = NodeState.READY
            summary.nodes_ready.append(node.id)

    def _process_node_never_ready(
        self, when: datetime, summary: AdvanceSummary
    ) -> None:
        due = (
            self.session.query(Node)
            .filter(
                Node.state == NodeState.BOOTING,
                Node.doomed_never_ready.is_(True),
                Node.never_ready_deadline <= when,
            )
            .all()
        )
        for node in due:
            node.state = NodeState.FAILED
            node.terminated_at = when
            billing.finalize_node_billing(self.session, node, when)
            summary.nodes_failed_never_ready.append(node.id)

    def _process_reclaim_teardown(
        self, when: datetime, summary: AdvanceSummary
    ) -> None:
        due = (
            self.session.query(Node)
            .filter(Node.state == NodeState.RECLAIMING, Node.reclaim_at <= when)
            .all()
        )
        for node in due:
            running_jobs = (
                self.session.query(Job)
                .filter(Job.node_id == node.id, Job.state == JobState.RUNNING)
                .all()
            )
            for job in running_jobs:
                self._accrue_running_time(job, when)
                job.node_id = None
                job.state = JobState.QUEUED
                job.requeue_count += 1
                self._log_event(
                    job,
                    JobState.QUEUED,
                    when,
                    note="requeued: interruptible node reclaimed",
                )
                summary.jobs_requeued_after_reclaim.append(job.id)
            node.state = NodeState.TERMINATED
            node.terminated_at = when
            node.gpus_free = node.gpus_total
            billing.finalize_node_billing(self.session, node, when)
            summary.nodes_reclaimed.append(node.id)

    def _process_job_completions(self, when: datetime, summary: AdvanceSummary) -> None:
        running = self.session.query(Job).filter(Job.state == JobState.RUNNING).all()
        for job in running:
            if job.started_at is None:
                continue
            due_at = job.started_at + timedelta(minutes=job.remaining_run_minutes)
            if due_at <= when:
                job.total_run_minutes_accrued += job.remaining_run_minutes
                job.remaining_run_minutes = 0.0
                # Replay jobs carry the trace's recorded outcome (succeeded,
                # failed, or cancelled); live jobs always succeed when their
                # countdown reaches zero -- see Job.replay_outcome.
                outcome = job.replay_outcome or JobState.SUCCEEDED
                job.state = outcome
                job.completed_at = due_at
                self._log_event(job, outcome, due_at)
                self._release_job_from_node(job, due_at)
                summary.jobs_completed.append(job.id)

    def _process_idle_reaper(self, when: datetime, summary: AdvanceSummary) -> None:
        for node in scheduler.find_idle_nodes_to_reap(
            self.session, when, self.idle_reaper_minutes
        ):
            node.state = NodeState.TERMINATED
            node.terminated_at = when
            billing.finalize_node_billing(self.session, node, when)
            summary.nodes_reaped_idle.append(node.id)

    def _process_heartbeat(self, when: datetime, summary: AdvanceSummary) -> None:
        if when < self._last_heartbeat_at + timedelta(
            minutes=HEARTBEAT_INTERVAL_MINUTES
        ):
            return
        self._last_heartbeat_at = when
        save_heartbeat_anchor(self.session, when)
        self.provider.tick_incident_state(when)
        ready_interruptible = (
            self.session.query(Node)
            .filter(Node.state == NodeState.READY, Node.is_interruptible.is_(True))
            .all()
        )
        for node in ready_interruptible:
            if self.provider.roll_reclaim_notice():
                node.state = NodeState.RECLAIMING
                node.reclaim_notice_at = when
                node.reclaim_at = when + self.provider.reclaim_notice_period()
