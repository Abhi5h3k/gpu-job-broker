"""Shared test fixtures.

Every test gets its own fresh in-memory SQLite database (fast, and fully
isolated between tests -- no shared state, no cleanup needed). Randomness
from the fake provider is made deterministic with :class:`ScriptedRandom`
so tests assert on exact behavior instead of "probably passes."
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.broker import Broker
from app.clock import SimulatedClock
from app.config import NODE_READY_MIN_MINUTES, SchedulingPolicy
from app.models import Base
from app.provider import FakeCloudProvider

# A fixed, readable start time used across all tests so assertions about
# elapsed time don't depend on wall-clock reality.
START = datetime(2025, 1, 1, 0, 0, 0)


class ScriptedRandom:
    """A deterministic stand-in for ``random.Random``, so tests can force
    a specific provider outcome (e.g. "this is one of the 3% that never
    becomes ready") instead of hunting for a seed that happens to produce
    it.

    ``random_values`` are consumed in order by ``.random()``; the last
    value repeats forever once the list is exhausted, so a test only needs
    to specify as many values as the interesting part of the scenario.
    Same idea for ``uniform_values`` and ``.uniform()``.
    """

    def __init__(
        self,
        random_values: list[float] | None = None,
        uniform_values: list[float] | None = None,
    ) -> None:
        self._random_values = list(random_values) if random_values else [0.99]
        self._uniform_values = list(uniform_values) if uniform_values else [NODE_READY_MIN_MINUTES]

    def random(self) -> float:
        return self._pop(self._random_values)

    def uniform(self, a: float, b: float) -> float:
        return self._pop(self._uniform_values)

    @staticmethod
    def _pop(queue: list[float]) -> float:
        value = queue[0]
        if len(queue) > 1:
            queue.pop(0)
        return value


def make_session():
    """A brand-new in-memory database with the schema created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)()


def make_provider(random_values=None, uniform_values=None) -> FakeCloudProvider:
    return FakeCloudProvider(rng=ScriptedRandom(random_values, uniform_values))


def fast_happy_provider() -> FakeCloudProvider:
    """Never fails capacity, never goes never-ready, never reclaims, always
    boots in the minimum time -- for tests that just want a predictable
    happy path without fighting randomness."""
    return make_provider(random_values=[0.99], uniform_values=[NODE_READY_MIN_MINUTES])


def make_broker(session=None, provider=None, now=START, **kwargs) -> Broker:
    """Build a Broker with sensible, deterministic defaults, overridable
    per test. Reused by every test module instead of a single fixture so
    tests can freely vary policy/provider/clock without fixture gymnastics.
    """
    session = session or make_session()
    provider = provider or fast_happy_provider()
    clock = SimulatedClock(now=now)
    return Broker(session, clock, provider, **kwargs)


@pytest.fixture
def session():
    return make_session()


@pytest.fixture
def broker(session):
    """The default broker: Balanced policy, a provider that always
    succeeds quickly. Most tests that just need a working end-to-end
    pipeline should use this."""
    return make_broker(session=session, policy=SchedulingPolicy.BALANCED)
