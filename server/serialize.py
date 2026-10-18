"""Turn rows into response dicts. Nothing secret is ever put in these (no answers, no password hashes)."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Job, LectureSession, iso


def user_out(user) -> dict:
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "email_verified": user.email_verified,
            "google_linked": user.google_sub is not None, "created_at": iso(user.created_at)}


def job_out(job: Job) -> dict:
    return {"id": job.id, "session_id": job.session_id, "kind": job.kind, "state": job.state, "stage": job.stage,
            "percent": job.percent, "message": job.message, "error_code": job.error_code,
            "error_message": job.error_message, "created_at": iso(job.created_at),
            "started_at": iso(job.started_at), "finished_at": iso(job.finished_at)}


def _duration(s: LectureSession) -> float | None:
    """Length of the lecture in seconds, known once the notes exist (the pipeline stores it in the note stats)."""
    if s.note is None:
        return None
    try:
        seconds = json.loads(s.note.stats_json or "{}").get("total_seconds")
    except ValueError:
        return None
    return round(float(seconds), 1) if isinstance(seconds, (int, float)) else None


def session_out(db: Session, s: LectureSession) -> dict:
    latest = db.scalar(select(Job).where(Job.session_id == s.id).order_by(Job.id.desc()).limit(1))
    return {"id": s.id, "module_id": s.module_id, "title": s.title, "state": s.state, "source": s.source,
            "has_slides": s.has_slides, "config": s.config, "created_at": iso(s.created_at),
            "files": [{"id": f.id, "kind": f.kind, "original_name": f.original_name, "bytes": f.bytes, "removed": f.removed} for f in s.files],
            "latest_job": job_out(latest) if latest else None,
            "has_notes": s.note is not None and s.state == "done", "duration_seconds": _duration(s),
            "replay_state": s.replay_state, "replay_kind": s.replay_kind, "visuals_state": s.visuals_state, "shot_count": s.shot_count}
