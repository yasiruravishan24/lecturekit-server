"""Engine and session factory. SQLite in WAL mode with foreign keys on (cascade deletes need them)."""
from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base
from .settings import Settings


def make_session_factory(settings: Settings) -> sessionmaker:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(engine)           # no migrations for now (SERVER_PLAN.md section 3)
    return sessionmaker(engine, expire_on_commit=False)


def get_db(request: Request):
    """FastAPI dependency: one DB session per request (the factory lives on app.state)."""
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
