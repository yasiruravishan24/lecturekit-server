"""Notifications kept on the server, so the web app, the extension and the mobile app all show the same list, and a new one
reaches every open client at once (events.py)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .events import Hub
from .models import LectureSession, Notification, iso

KEEP = 50                                         # per user; older ones are removed

TEXT = {
    "recording_started": "Recording started",
    "processing_started": "Making your notes",
    "notes_ready": "Notes are ready",
    "failed": "Processing failed",
    "replay_ready": "Replay and key moments are ready",
}


def out(n: Notification) -> dict:
    return {"id": n.id, "kind": n.kind, "session_id": n.session_id, "title": n.title, "message": n.message,
            "created_at": iso(n.created_at), "read": bool(n.read)}


def add(db: Session, hub: Hub | None, user_id: int, session: LectureSession | None, kind: str, message: str = "") -> Notification:
    """Save a notification for the user and push it to every client that is open. Commits."""
    row = Notification(user_id=user_id, session_id=session.id if session is not None else None, kind=kind,
                       title=session.title if session is not None else "", message=message or TEXT.get(kind, kind))
    db.add(row)
    db.commit()
    old = db.scalars(select(Notification.id).where(Notification.user_id == user_id).order_by(Notification.id.desc()).offset(KEEP)).all()
    if old:
        for n in db.scalars(select(Notification).where(Notification.id.in_(old))).all():
            db.delete(n)
        db.commit()
    if hub is not None:
        hub.publish(user_id, {"type": "notification", "notification": out(row)})
    return row
