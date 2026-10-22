"""Creating jobs: the daily limit, the job row, the queue entry (SERVER_PLAN.md sections 6 and 8)."""
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import notifications
from .errors import ApiError
from .models import Job, LectureSession, User, utcnow


def check_daily_limit(db: Session, user: User, limit: int) -> None:
    """Counts `process` and `finish` jobs created in the last 24 hours (chunk jobs are free). A job lost to a server restart, or one the person cancelled, is not
    counted."""
    since = utcnow() - timedelta(hours=24)
    used = db.scalar(select(func.count(Job.id)).where(
        Job.user_id == user.id, Job.kind.in_(("process", "finish")), Job.created_at >= since, Job.state != "cancelled",
        or_(Job.error_code.is_(None), Job.error_code != "server_restarted"))) or 0
    if used >= limit:
        raise ApiError(429, "daily_limit", f"Daily limit reached ({limit} lectures); try again tomorrow")


def enqueue(request: Request, db: Session, user: User, session: LectureSession, kind: str, arg: int | None = None) -> Job:
    """Create the job row first, then queue it. process and finish also move the session to `queued`."""
    settings = request.app.state.settings
    if kind in ("process", "finish"):
        if settings.check_daily_limit:
            check_daily_limit(db, user, settings.daily_limit)
        session.state = "queued"
    job = Job(session_id=session.id, user_id=user.id, kind=kind, arg=arg)
    db.add(job)
    db.commit()
    if kind in ("process", "finish"):
        notifications.add(db, request.app.state.hub, user.id, session, "processing_started")
    request.app.state.worker.submit(job.id)
    return job


def active(session: LectureSession) -> bool:
    return session.state in ("queued", "processing")


def background_busy(session: LectureSession) -> bool:
    """The pictures or the replay of this lecture are still being made (they read the recording)."""
    return session.visuals_state in ("queued", "processing") or session.replay_state in ("queued", "processing") or session.deeper_state in ("queued", "processing")
