"""SQLAlchemy tables (SERVER_PLAN.md section 5).

Deleting a user, course, module or session deletes the children (ORM cascade plus ON DELETE CASCADE).
Job rows are kept for the history of failures when a session is deleted (session_id becomes NULL);
they go when the user goes.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)      # naive UTC, serialised with iso()


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class Base(DeclarativeBase):
    pass


def _fk(target: str, null: bool = False, on_delete: str = "CASCADE"):
    return mapped_column(Integer, ForeignKey(target, ondelete=on_delete), nullable=null, index=True)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    courses = relationship("Course", cascade="all, delete-orphan", passive_deletes=True)
    jobs = relationship("Job", cascade="all, delete-orphan", passive_deletes=True, foreign_keys="Job.user_id")


class Course(Base):
    __tablename__ = "course"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = _fk("user.id")
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modules = relationship("Module", cascade="all, delete-orphan", passive_deletes=True, order_by="Module.id")


class Module(Base):
    __tablename__ = "module"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = _fk("course.id")
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sessions = relationship("LectureSession", cascade="all, delete-orphan", passive_deletes=True, order_by="LectureSession.id")


class LectureSession(Base):
    """One lecture: its files, jobs, notes and quiz. (Table name `session` as in the plan.)"""
    __tablename__ = "session"
    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[int] = _fk("module.id")
    user_id: Mapped[int] = _fk("user.id")
    title: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(16), default="empty")    # empty uploaded live queued processing done failed
    has_slides: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(8), default="upload")   # upload | live
    config: Mapped[str | None] = mapped_column(String(1), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    files = relationship("MediaFile", cascade="all, delete-orphan", passive_deletes=True, order_by="MediaFile.id")
    live = relationship("LiveUpload", cascade="all, delete-orphan", passive_deletes=True, uselist=False)
    note = relationship("Note", cascade="all, delete-orphan", passive_deletes=True, uselist=False)
    questions = relationship("Question", cascade="all, delete-orphan", passive_deletes=True, order_by="Question.position")
    attempts = relationship("Attempt", cascade="all, delete-orphan", passive_deletes=True, order_by="Attempt.id")


class MediaFile(Base):
    __tablename__ = "media_file"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = _fk("session.id")
    kind: Mapped[str] = mapped_column(String(10))                      # recording | slides
    original_name: Mapped[str] = mapped_column(String(255))            # text only, never used as a path
    stored_name: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = _fk("session.id", null=True, on_delete="SET NULL")
    user_id: Mapped[int] = _fk("user.id")
    kind: Mapped[str] = mapped_column(String(8))                       # process | chunk | finish
    state: Mapped[str] = mapped_column(String(10), default="queued")   # queued processing done failed
    stage: Mapped[str] = mapped_column(String(16), default="queued")
    percent: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    arg: Mapped[int | None] = mapped_column(Integer, nullable=True)    # chunk index for kind=chunk, total for kind=finish
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LiveUpload(Base):
    __tablename__ = "live_upload"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("session.id", ondelete="CASCADE"), unique=True)
    chunk_seconds: Mapped[int] = mapped_column(Integer)
    overlap: Mapped[int] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    chunks = relationship("LiveChunk", cascade="all, delete-orphan", passive_deletes=True, order_by="LiveChunk.idx")


class LiveChunk(Base):
    __tablename__ = "live_chunk"
    __table_args__ = (UniqueConstraint("live_id", "idx"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    live_id: Mapped[int] = _fk("live_upload.id")
    idx: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(12), default="received")  # received | transcribed | failed
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Note(Base):
    __tablename__ = "note"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("session.id", ondelete="CASCADE"), unique=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    full_notes_json: Mapped[str] = mapped_column(Text)
    short_notes_json: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(64), default="")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Question(Base):
    __tablename__ = "question"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = _fk("session.id")
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text)
    answer_index: Mapped[int] = mapped_column(Integer)                 # never sent before an attempt is submitted
    bloom_level: Mapped[str] = mapped_column(String(16), default="")
    explanation: Mapped[str] = mapped_column(Text, default="")         # never sent before an attempt is submitted


class Attempt(Base):
    __tablename__ = "attempt"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = _fk("session.id")
    user_id: Mapped[int] = _fk("user.id")
    score: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    answers = relationship("AttemptAnswer", cascade="all, delete-orphan", passive_deletes=True)


class AttemptAnswer(Base):
    __tablename__ = "attempt_answer"
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = _fk("attempt.id")
    question_id: Mapped[int] = _fk("question.id")
    chosen_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correct: Mapped[bool] = mapped_column(Boolean)
