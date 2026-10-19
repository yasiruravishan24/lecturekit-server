"""Go deeper: background, examples, common mistakes, things to check and pages to read for every topic, plus extra quiz questions.
Made on request, in the background lane; see lecturekit/enrich.py."""
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..access import own_session
from ..db import get_db
from ..errors import conflict, unprocessable
from ..jobs import active, background_busy
from ..models import Job, User
from ..security import current_user
from ..validation import Id

router = APIRouter(tags=["deeper"])


@router.get("/sessions/{session_id}/deeper")
def get_deeper(session_id: Id, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """{"state": none|queued|processing|done|failed, "percent": int|None, "data": {web, topics, not_covered, questions...}|None}"""
    s = own_session(db, user, session_id)
    data = None
    if s.deeper_state == "done":
        path = storage.session_dir(request.app.state.settings, s.user_id, s.id) / "deeper.json"
        if path.is_file():
            data = json.loads(path.read_text())
    job = db.scalar(select(Job).where(Job.session_id == s.id, Job.kind == "deeper").order_by(Job.id.desc()).limit(1))
    percent = job.percent if job is not None and job.state in ("queued", "processing") else None
    return {"state": s.deeper_state, "percent": percent, "stage": job.stage if percent is not None else None, "data": data}


@router.post("/sessions/{session_id}/deeper")
def start_deeper(session_id: Id, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = own_session(db, user, session_id)
    if s.state != "done" or s.note is None:
        raise unprocessable("Learn more can be made once the notes are ready", "validation")
    if active(s) or background_busy(s):
        raise conflict("This lecture is still being worked on; try again when it has finished", "busy")
    if s.deeper_state == "done":
        raise conflict("Learn more is already there", "deeper_exists")
    return {"job_id": request.app.state.worker.start_deeper(db, s)}
