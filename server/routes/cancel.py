"""Cancel: stop what is being made for a lecture. Notes in the making: the lecture and its recording are deleted. An extra (pictures,
replay, learn more): only that stops, the lecture and its notes stay."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..access import own_session
from ..db import get_db
from ..errors import conflict
from ..events import sync
from ..models import Job, User, utcnow
from ..security import current_user
from ..validation import Id

router = APIRouter(tags=["jobs"])


@router.post("/sessions/{session_id}/cancel")
def cancel(session_id: Id, request: Request, kind: str = Query("main", pattern="^(main|visuals|replay|deeper)$"),
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    """kind=main (the default): the notes and the quiz. visuals, replay, deeper: the extras.

    Answers {"state": "deleted"} (a lecture that had not started: gone at once), {"state": "cancelling"} (it stops within seconds and the
    lecture is then deleted, or the extra stops) or {"state": "cancelled"} (an extra that had not started)."""
    session = own_session(db, user, session_id)
    worker, hub = request.app.state.worker, request.app.state.hub
    kinds = ("process", "finish") if kind == "main" else (kind,)
    jobs = db.scalars(select(Job).where(Job.session_id == session.id, Job.kind.in_(kinds), Job.state.in_(("queued", "processing")))).all()
    if not jobs:
        raise conflict("Nothing is being made right now", "nothing_to_cancel")

    running = [j for j in jobs if j.state == "processing"]
    for j in running:
        worker.request_cancel(j.id)
        j.stage = "cancelling"
    if running:
        db.commit()
        if hub is not None:
            hub.publish(user.id, {"type": "job", "session_id": session.id, "kind": running[0].kind, "stage": "cancelling", "percent": running[0].percent or 0, "message": ""})
        return {"state": "cancelling"}

    # not started yet: nothing to stop, only to put right
    for j in jobs:
        j.state, j.finished_at, j.stage = "cancelled", utcnow(), "cancelled"
    if kind == "main":
        user_id, sid = session.user_id, session.id
        db.delete(session)                                   # the job rows stay (cancelled, not counted); the files go
        db.commit()
        storage.remove_tree(storage.session_dir(request.app.state.settings, user_id, sid))
        sync(hub, user.id, "cancelled", sid)
        return {"state": "deleted"}
    worker.reset_extra(session, kind)
    db.commit()
    sync(hub, user.id, "cancelled", session.id)
    return {"state": "cancelled"}
