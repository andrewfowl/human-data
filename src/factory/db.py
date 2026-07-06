"""Database engine and session management."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False}
            if settings.database_url.startswith("sqlite")
            else {},
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    from . import models  # noqa: F401 — register mappings

    Base.metadata.create_all(get_engine())


def session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def reset_for_tests(url: str) -> None:
    """Point the module at a fresh database (test isolation)."""
    global _engine, _SessionLocal
    settings.database_url = url
    _engine = None
    _SessionLocal = None
    init_db()
