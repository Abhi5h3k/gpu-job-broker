"""FastAPI dependency-injection glue: one DB session per request, and a
Broker built from that session plus the process-wide clock/provider.

This tiny module exists only to avoid a circular import between
app/main.py (which creates the clock/provider and includes the routers)
and app/api/*.py (which needs a Broker to do anything) -- it is pure
plumbing, not a new concept in the system.
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from app.broker import Broker
from app.db import SessionLocal


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_broker(request: Request, session: Session) -> Broker:
    """Build a Broker for this request, reusing the single process-wide
    SimulatedClock and FakeCloudProvider instances stored on app.state at
    startup (see app/main.py's lifespan)."""
    return Broker(session, request.app.state.clock, request.app.state.provider)
