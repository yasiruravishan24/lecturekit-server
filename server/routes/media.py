"""Watching the lecture again and looking at the board pictures (screenshots): signed links, HTTP Range so seeking works."""
import mimetypes

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import storage
from ..access import own_session
from ..db import get_db
from ..errors import conflict, not_found, unprocessable
from ..jobs import active, background_busy
from ..models import Job, LectureSession, MediaFile, Shot, User
from ..security import create_media_token, current_user, read_media_token
from ..validation import Id

router = APIRouter(tags=["media"])


def storage_used(db: Session, user_id: int) -> int:
    """Bytes a user keeps on the server: recordings not yet replaced by their replay, slides, and the replays."""
    files = db.scalar(select(func.coalesce(func.sum(MediaFile.bytes), 0)).join(LectureSession, MediaFile.session_id == LectureSession.id)
                      .where(LectureSession.user_id == user_id, MediaFile.removed.is_(False))) or 0
    replays = db.scalar(select(func.coalesce(func.sum(LectureSession.replay_bytes), 0)).where(LectureSession.user_id == user_id)) or 0
    return int(files) + int(replays)


def _link(request: Request, token: str) -> str:
    return f"{request.app.state.settings.api_prefix}/media/{token}"


def _latest_percent(db: Session, session_id: int, kind: str) -> int | None:
    job = db.scalar(select(Job).where(Job.session_id == session_id, Job.kind == kind).order_by(Job.id.desc()).limit(1))
    return job.percent if job is not None and job.state in ("queued", "processing") else None


@router.get("/sessions/{session_id}/replay")
def replay_info(session_id: Id, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """State of the replay (queued, processing with a percent, ready with a link, failed) and of the board pictures."""
    s = own_session(db, user, session_id)
    settings = request.app.state.settings
    url = None
    if s.replay_state == "ready" and storage.find_replay(storage.session_dir(settings, s.user_id, s.id)) is not None:
        url = _link(request, create_media_token(settings, user.id, s.id, "replay"))
    return {"state": s.replay_state, "kind": s.replay_kind, "bytes": s.replay_bytes, "url": url,
            "percent": _latest_percent(db, s.id, "replay"), "visuals_state": s.visuals_state,
            "visuals_percent": _latest_percent(db, s.id, "visuals")}


@router.post("/sessions/{session_id}/replay")
def make_replay(session_id: Id, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Make the board pictures and the replay now: for a lecture that was processed before they existed, or after a failure.
    Needs the original recording to still be on the server. Runs in the background; poll GET .../replay."""
    s = own_session(db, user, session_id)
    if active(s) or background_busy(s):
        raise conflict("This lecture is still being worked on; try again when it has finished", "busy")
    if s.state != "done":
        raise unprocessable("The replay can be made once the notes are ready", "validation")
    if s.replay_state == "ready":
        raise conflict("The replay is already there", "replay_exists")
    sdir = storage.session_dir(request.app.state.settings, s.user_id, s.id)
    parts = s.source == "live" and any(storage.upload_dir(sdir).glob("chunk_*"))        # a recording made with the extension
    if not parts and not any(f.kind == "recording" and not f.removed for f in s.files):
        raise unprocessable("The original recording is no longer on the server, so there is nothing to make a replay from", "missing_input")
    job_id = request.app.state.worker.start_background(db, s)
    return {"job_id": job_id}


@router.get("/sessions/{session_id}/shots")
def list_shots(session_id: Id, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The pictures of the board and screen, in time order, each with a signed link to the image."""
    s = own_session(db, user, session_id)
    settings = request.app.state.settings
    return [{"id": x.id, "t": x.t, "kind": x.kind, "text": x.text, "description": x.description,
             "url": _link(request, create_media_token(settings, user.id, s.id, "shot", x.id))} for x in s.shots]


@router.get("/media/{token}", include_in_schema=False)
def media(token: str, request: Request, download: bool = False, db: Session = Depends(get_db)):
    """Serves a replay or a screenshot to whoever holds a valid signed link (no Authorization header needed)."""
    settings = request.app.state.settings
    claims = read_media_token(settings, token)
    if claims is None:
        raise not_found()
    s = db.scalar(select(LectureSession).where(LectureSession.id == claims["sid"], LectureSession.user_id == int(claims["sub"])))
    if s is None:
        raise not_found()
    sdir = storage.session_dir(settings, s.user_id, s.id)
    if claims["what"] == "replay":
        path, name = storage.find_replay(sdir), f"{s.title}{'.mp4' if s.replay_kind == 'video' else '.m4a'}"
    else:
        shot = db.scalar(select(Shot).where(Shot.id == claims["id"], Shot.session_id == s.id))
        path, name = (storage.shots_dir(sdir) / shot.file, f"{s.title} {int(shot.t // 60):02d}-{int(shot.t % 60):02d}.jpg") if shot else (None, "")
    if path is None or not path.exists():
        raise not_found()
    kind = "audio/mp4" if path.suffix == ".m4a" else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=kind, filename=name if download else None, content_disposition_type="attachment" if download else "inline")
