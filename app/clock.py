"""The simulated clock: the one thing every other module asks for the
current time instead of calling ``datetime.now()`` directly.

Design decision (see docs/DESIGN.md for the full reasoning): this clock is
purely manual/event-driven. Nothing in this codebase ever sleeps, and
nothing ticks on a background thread. Time only ever moves because
something calls :meth:`SimulatedClock.set`, directly or via
``POST /simulator/advance``. This is what makes "it must run in seconds"
true for both the trace replay (a week of activity replays in a fraction
of a real second) and the live API (you choose how fast a "minute" goes
by calling advance with whatever step size you want).

The clock's current time is persisted to the ``clock_state`` table so a
restart resumes exactly where it left off (requirement 7).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import ClockState

_CLOCK_ROW_ID = 1


class SimulatedClock:
    """A single mutable point in simulated time, backed by one database row."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    @property
    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        """Move the clock forward. Refusing to move it backward keeps every
        other module's "has this already happened" checks simple."""
        if when < self._now:
            raise ValueError(f"Clock cannot move backward: {when} < {self._now}")
        self._now = when

    def advance_minutes(self, minutes: float) -> datetime:
        """Convenience: move forward by a relative number of minutes and
        return the new time. Most callers want :meth:`set` with an
        absolute target instead (see broker.py's event loop), but this is
        handy for tests and simple scripts."""
        self.set(self._now + timedelta(minutes=minutes))
        return self._now


def load_clock(session: Session, default_start: datetime) -> SimulatedClock:
    """Load the persisted clock time, creating the single row with
    ``default_start`` if this is a brand-new database (first-ever run)."""
    row = session.get(ClockState, _CLOCK_ROW_ID)
    if row is None:
        # Seed last_heartbeat_at alongside now, both to default_start, so
        # the heartbeat anchor has a fixed origin from the very first run
        # instead of re-defaulting to "whatever time it is right now" on
        # every subsequent request until the first heartbeat happens to
        # fire -- see load_heartbeat_anchor's docstring.
        row = ClockState(
            id=_CLOCK_ROW_ID, now=default_start, last_heartbeat_at=default_start
        )
        session.add(row)
        session.commit()
    return SimulatedClock(row.now)


def save_clock(session: Session, clock: SimulatedClock) -> None:
    """Persist the clock's current time. Called at the end of every
    operation that might have advanced it, so a crash/restart never loses
    more than the in-flight call."""
    row = session.get(ClockState, _CLOCK_ROW_ID)
    if row is None:
        row = ClockState(id=_CLOCK_ROW_ID, now=clock.now)
        session.add(row)
    else:
        row.now = clock.now
    session.commit()


def load_heartbeat_anchor(session: Session, default: datetime) -> datetime:
    """Load the persisted heartbeat anchor (see broker.py's
    HEARTBEAT_INTERVAL_MINUTES), falling back to ``default`` if this is a
    brand-new database or no heartbeat has ever fired yet.

    This exists because a :class:`~app.broker.Broker` is reconstructed
    fresh on every API request. Without persisting this anchor the same
    way the clock's own time is persisted, each new ``Broker`` would reset
    its heartbeat cadence to "now," and a live caller advancing the clock
    in small steps (or across many separate requests) could see the
    periodic reclaim-notice/capacity-retry check never fire at all."""
    row = session.get(ClockState, _CLOCK_ROW_ID)
    if row is None or row.last_heartbeat_at is None:
        return default
    return row.last_heartbeat_at


def save_heartbeat_anchor(session: Session, when: datetime) -> None:
    """Persist the last time the heartbeat actually fired, for the same
    restart/per-request-reconstruction reason :func:`save_clock` persists
    the clock's time."""
    row = session.get(ClockState, _CLOCK_ROW_ID)
    if row is None:
        row = ClockState(id=_CLOCK_ROW_ID, now=when, last_heartbeat_at=when)
        session.add(row)
    else:
        row.last_heartbeat_at = when
    session.commit()


def load_capacity_blocked(session: Session, default: bool = False) -> bool:
    """Load whether the last scheduling pass left a job genuinely stuck on
    a provider-side :class:`~app.provider.CapacityUnavailableError` (see
    :func:`save_capacity_blocked`). Falls back to ``default`` for a
    brand-new database or a session that never persisted this."""
    row = session.get(ClockState, _CLOCK_ROW_ID)
    if row is None or row.capacity_blocked is None:
        return default
    return row.capacity_blocked


def save_capacity_blocked(session: Session, blocked: bool, now: datetime) -> None:
    """Persist whether a job is currently stuck purely on
    CapacityUnavailableError, so the heartbeat anchor's "should I keep
    polling" decision survives the Broker being reconstructed fresh on
    every API request. ``now`` only seeds a brand-new row's ``now`` column
    in the (practically unreachable in production, since app/main.py
    always calls :func:`load_clock` first) case where this fires before
    any ``ClockState`` row exists yet."""
    row = session.get(ClockState, _CLOCK_ROW_ID)
    if row is None:
        row = ClockState(id=_CLOCK_ROW_ID, now=now, capacity_blocked=blocked)
        session.add(row)
    else:
        row.capacity_blocked = blocked
    session.commit()
