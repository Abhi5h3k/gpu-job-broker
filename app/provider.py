"""The fake cloud provider: everything the real world would make slow,
unreliable, or expensive, simulated as simple dice rolls against the
simulated clock. Nothing here does any real I/O or sleeping.

This module owns exactly the two behaviours the real provider would have
that the broker can't control:

1. Whether a launch attempt is granted at all (:meth:`request_capacity`) --
   can fail with bursty ``CapacityUnavailable``, *before* any node or
   billing exists.
2. What happens to a granted launch over time (:meth:`plan_launch`,
   :meth:`roll_reclaim_notice`) -- how long it takes to become ready,
   whether it's one of the unlucky ~3% that never comes up, and whether an
   interruptible node gets reclaimed.

See docs/DESIGN.md for the full reasoning behind each calibration choice
below; the short version is in each docstring.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import (
    CAPACITY_FAILURE_PROBABILITY_DURING_INCIDENT,
    CAPACITY_FAILURE_PROBABILITY_NORMAL,
    CAPACITY_INCIDENT_MAX_DURATION_MINUTES,
    CAPACITY_INCIDENT_MIN_DURATION_MINUTES,
    CAPACITY_INCIDENT_START_PROBABILITY_PER_MINUTE,
    INTERRUPTIBLE_RECLAIM_NOTICE_MINUTES,
    INTERRUPTIBLE_RECLAIM_TARGET_FRACTION,
    NEVER_READY_PROBABILITY,
    NEVER_READY_REPORT_AFTER_MINUTES,
    NODE_READY_MAX_MINUTES,
    NODE_READY_MIN_MINUTES,
)

# Per-minute hazard that a ready, interruptible node gets a reclaim notice.
# Calibrated so that, over any hour a node stays up, the chance it
# experiences at least one reclaim is approximately the doc's "about 5% of
# node-hours" figure: 60 minutes * (0.05 / 60) = 0.05 expected events/hour.
# This is a deliberately simple, directly-interpretable calibration, not a
# precise statistical fit -- the doc itself says "model it as you see fit."
RECLAIM_HAZARD_PER_MINUTE = INTERRUPTIBLE_RECLAIM_TARGET_FRACTION / 60.0


class CapacityUnavailableError(Exception):
    """Raised by :meth:`FakeCloudProvider.request_capacity` when the
    provider has no capacity to give right now. No node is created and
    nothing is billed -- the caller (broker.py) should leave the job
    queued and simply try again later; this is never a job failure."""


@dataclass
class LaunchPlan:
    """What happens to a launch that *was* granted capacity: when it's due
    to become ready, and whether it's secretly doomed to never get there."""

    ready_at: datetime
    doomed_never_ready: bool
    never_ready_deadline: datetime | None  # set only if doomed_never_ready


class FakeCloudProvider:
    """One instance of this is created per running process (see
    broker.py) and lives for the process's lifetime. Its internal
    incident/burst state is intentionally *not* persisted across restarts:
    it's transient randomness bookkeeping, not job/node/cost state, so
    losing it on restart has no correctness impact -- worst case we
    slightly under- or over-simulate a burst right at the restart boundary.
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._incident_active = False
        self._incident_until: datetime | None = None

    # ---- CapacityUnavailable (bursty, pre-billing) -----------------------

    def request_capacity(self, now: datetime) -> None:
        """Roll the dice on whether a launch attempt is granted right now.
        Raises :class:`CapacityUnavailableError` if not. Call this *before*
        creating any Node row -- a rejection here means nothing was ever
        launched and nothing is billed.

        Whether a capacity *incident* is currently active is tracked
        separately from this call -- see :meth:`tick_incident_state`,
        which the broker's heartbeat rolls once per simulated minute. That
        split is what makes CAPACITY_INCIDENT_START_PROBABILITY_PER_MINUTE
        an actual per-minute hazard: this method only ever *reads*
        ``self._incident_active``, it never rolls for it, so the incident
        rate doesn't depend on how often (or how many times in the same
        instant) a launch happens to be attempted.
        """
        failure_probability = (
            CAPACITY_FAILURE_PROBABILITY_DURING_INCIDENT
            if self._incident_active
            else CAPACITY_FAILURE_PROBABILITY_NORMAL
        )
        if self._rng.random() < failure_probability:
            raise CapacityUnavailableError(
                "incident" if self._incident_active else "isolated capacity shortfall"
            )

    def tick_incident_state(self, now: datetime) -> None:
        """Advance the bursty capacity-incident state machine by exactly
        one simulated minute. Called once per minute from the broker's
        heartbeat (see HEARTBEAT_INTERVAL_MINUTES in broker.py), regardless
        of whether any launch is attempted that minute -- mirroring how
        :meth:`roll_reclaim_notice` is already rolled once per minute per
        node, rather than once per unrelated event.

        Bursty failures are modeled as a simple two-state process: most of
        the time we're in a 'normal' state with rare, isolated failures;
        occasionally a 'capacity incident' starts and persists for a short
        random span, during which failures are much more likely. This
        directly implements the doc's "such problems tend to come in
        bursts, not as isolated failures."
        """
        if self._incident_active:
            if self._incident_until is not None and now >= self._incident_until:
                self._incident_active = False
                self._incident_until = None
            return

        if self._rng.random() < CAPACITY_INCIDENT_START_PROBABILITY_PER_MINUTE:
            duration = self._rng.uniform(
                CAPACITY_INCIDENT_MIN_DURATION_MINUTES,
                CAPACITY_INCIDENT_MAX_DURATION_MINUTES,
            )
            self._incident_active = True
            self._incident_until = now + timedelta(minutes=duration)

    # ---- Readiness / the 3% never-ready case -----------------------------

    def plan_launch(self, now: datetime) -> LaunchPlan:
        """Decide, at launch time, how this *already-granted* launch will
        play out. Billing has already started by the time this is called
        (the caller creates the Node row with launched_at=now first) --
        this only decides the readiness timeline.
        """
        if self._rng.random() < NEVER_READY_PROBABILITY:
            return LaunchPlan(
                ready_at=now
                + timedelta(minutes=NODE_READY_MAX_MINUTES),  # never actually reached
                doomed_never_ready=True,
                never_ready_deadline=now
                + timedelta(minutes=NEVER_READY_REPORT_AFTER_MINUTES),
            )
        ready_in_minutes = self._rng.uniform(
            NODE_READY_MIN_MINUTES, NODE_READY_MAX_MINUTES
        )
        return LaunchPlan(
            ready_at=now + timedelta(minutes=ready_in_minutes),
            doomed_never_ready=False,
            never_ready_deadline=None,
        )

    # ---- Interruptible reclaim --------------------------------------------

    def roll_reclaim_notice(self) -> bool:
        """Called once per simulated minute, per ready interruptible node,
        by the broker's event loop. Returns True if this is the minute a
        reclaim notice fires for that node. See RECLAIM_HAZARD_PER_MINUTE
        for the calibration.
        """
        return self._rng.random() < RECLAIM_HAZARD_PER_MINUTE

    @staticmethod
    def reclaim_notice_period() -> timedelta:
        """Doc: interruptible nodes 'can be taken back with 2 minutes'
        notice' -- the grace period between the notice firing and the node
        actually being torn down."""
        return timedelta(minutes=INTERRUPTIBLE_RECLAIM_NOTICE_MINUTES)
