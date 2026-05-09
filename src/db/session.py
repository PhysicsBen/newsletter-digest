from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.config import settings
from src.db.models import Base

_engine = None
_SessionFactory = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, echo=False)
        if settings.database_url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
    return _engine


def _get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _SessionFactory


def init_db() -> None:
    """Create all tables. Safe to call multiple times (uses CREATE IF NOT EXISTS)."""
    Base.metadata.create_all(_get_engine())


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session. Rolls back on exception, always closes."""
    factory = _get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
