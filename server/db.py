"""Engine and session factory. SQLite in WAL mode with foreign keys on (cascade deletes need them)."""
from fastapi import Request
from sqlalchemy import create_engine, event, inspect, text
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

    Base.metadata.create_all(engine)
    _add_new_user_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _add_new_user_columns(engine) -> None:
    """A database made before sign-up details existed gets the new columns; its accounts count as verified."""
    columns = {c["name"] for c in inspect(engine).get_columns("user")}
    added = {"full_name": "VARCHAR(120) NOT NULL DEFAULT ''", "email_verified": "BOOLEAN NOT NULL DEFAULT 0",
             "google_sub": "VARCHAR(64)", "terms_accepted_at": "DATETIME",
             "otp_hash": "VARCHAR(64)", "otp_expires_at": "DATETIME", "otp_attempts": "INTEGER NOT NULL DEFAULT 0"}
    with engine.begin() as conn:
        for name, ddl in added.items():
            if name not in columns:
                conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {name} {ddl}'))
        if "email_verified" not in columns:
            conn.execute(text('UPDATE "user" SET email_verified = 1'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_google_sub ON "user" (google_sub)'))


def get_db(request: Request):
    """FastAPI dependency: one DB session per request (the factory lives on app.state)."""
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
