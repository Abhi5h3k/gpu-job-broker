"""SQLite engine/session setup.

This is the entire persistence layer: one SQLite file, one SQLAlchemy
engine, plain sessions. There is no migration framework -- ``init_db()``
just calls ``create_all()``, which is the right amount of ceremony for a
single-table-family SQLite database that one process owns.

Restart safety (requirement 7) falls out of this almost for free: every
job, node, usage segment, and the clock's current simulated time are rows
in this database. Stopping and restarting the process just means closing
and reopening the same file -- nothing in memory is load-bearing.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _make_engine():
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI's test client and request handlers
    # may touch the connection from different threads under uvicorn's
    # default worker model; we still only ever use one short-lived session
    # per request/call, so this is safe here.
    return create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )


engine = _make_engine()
# autoflush=True (the default) matters a lot here: broker.py mutates ORM
# objects in Python and then immediately runs filter() queries expecting
# to see those changes (e.g. "find READY nodes with free GPUs" right after
# placing a job on one). Without autoflush, such queries could see stale
# pre-mutation data straight from the database.
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


def init_db() -> None:
    """Create all tables if they don't already exist. Safe to call every
    startup -- it's a no-op on an existing database file."""
    from app import models

    models.Base.metadata.create_all(engine)


def get_session() -> Session:
    """Return a new session. Callers are responsible for closing it (the
    API layer does this via FastAPI's dependency-injection `with` pattern
    in app/api/*; the broker and replay driver use explicit `with` blocks)."""
    return SessionLocal()
