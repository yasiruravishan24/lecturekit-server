"""Live recording in chunks (SERVER_PLAN.md sections 1 and 7). The chunk contract lives in lecturekit.chunks."""
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.orm import Session

from lecturekit import api as lk
from lecturekit.chunks import CHUNK_SECONDS, OVERLAP

from .. import storage
from ..access import own_session
from ..db import get_db
from ..errors import ApiError, conflict, unprocessable
from ..jobs import enqueue
from ..models import LiveChunk, LiveUpload, User
from ..validation import Id
from ..security import current_user
from .uploads import commit_files, stage_files

router = APIRouter(tags=["live"])
OPEN_STATES = ("live", "failed")        # after a failed finish the client may re-send chunks and finish again


def _live_session(db: Session, user: User, session_id: Id):
    session = own_session(db, user, session_id)
    if session.source != "live" or session.live is None:
        raise conflict("Open a live recording first", "not_live")
    return session


def _missing_message(missing: list[int]) -> str:
    nums = [str(k) for k in missing]
    if len(nums) == 1:
        return f"chunk {nums[0]} is missing, send it and finish again"
    return f"chunks {', '.join(nums[:-1])} and {nums[-1]} are missing, send them and finish again"


def _status(live: LiveUpload, total: int | None) -> dict:
    rows = {c.idx: c for c in live.chunks}
    total = total or live.total
    missing = [k for k in range(total) if k not in rows or rows[k].state == "failed"] if total else []
    return {"chunk_seconds": live.chunk_seconds, "overlap": live.overlap, "total": total,
            "received": sorted(rows), "transcribed": sorted(k for k, c in rows.items() if c.state == "transcribed"),
            "failed": sorted(k for k, c in rows.items() if c.state == "failed"), "missing": missing}


@router.post("/sessions/{session_id}/live")
def open_live(session_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = own_session(db, user, session_id)
    if session.live is None:
        if session.state != "empty":
            raise conflict("This lecture already has a recording", "not_empty")
        session.live = LiveUpload(chunk_seconds=CHUNK_SECONDS, overlap=OVERLAP)
        session.source, session.state = "live", "live"
        db.commit()
    return {"chunk_seconds": session.live.chunk_seconds, "overlap": session.live.overlap}


@router.post("/sessions/{session_id}/live/chunks")
def upload_chunk(session_id: Id, request: Request, index: int = Query(ge=0, le=1_000_000), chunk: UploadFile = File(...),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Saved at once, Whisper queued. The same index again redoes that chunk."""
    session = _live_session(db, user, session_id)
    settings = request.app.state.settings
    if session.state not in OPEN_STATES:
        raise conflict("This live recording is already being processed", "busy")
    existing = next((c for c in session.live.chunks if c.idx == index), None)
    if existing is None and (index >= settings.max_chunks_per_session or len(session.live.chunks) >= settings.max_chunks_per_session):
        raise ApiError(429, "chunk_limit", f"Too many chunks (limit {settings.max_chunks_per_session})")
    ext = storage.check_extension(chunk.filename, lk.MEDIA_TYPES, "chunk")
    updir = storage.upload_dir(storage.session_dir(settings, user.id, session.id))
    tmp = updir / f".chunk_{index:05d}.part"
    storage.save_upload(chunk, tmp, settings.max_chunk_mb)
    for old in updir.glob(f"chunk_{index:05d}.*"):
        old.unlink(missing_ok=True)
    tmp.replace(updir / f"chunk_{index:05d}{ext}")
    if existing is None:
        session.live.chunks.append(LiveChunk(idx=index, state="received"))
    else:
        existing.state, existing.seconds, existing.error_message = "received", None, None
    session.state = "live"
    db.commit()
    job = enqueue(request, db, user, session, "chunk", index)
    return {"queued": index, "job_id": job.id}


@router.get("/sessions/{session_id}/live")
def live_status(session_id: Id, total: int | None = Query(None, ge=1, le=1000), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    return _status(_live_session(db, user, session_id).live, total)


@router.post("/sessions/{session_id}/live/finish")
def finish(session_id: Id, request: Request, total: int = Query(ge=1, le=1000), slides: list[UploadFile] = File(default=[]),
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    """After Stop: all chunks must have arrived (they may still be queued for Whisper: the queue is FIFO)."""
    session = _live_session(db, user, session_id)
    if session.state not in OPEN_STATES:
        raise conflict("This live recording is already being processed", "busy")
    if total > request.app.state.settings.max_chunks_per_session:
        raise unprocessable(f"A recording can have at most {request.app.state.settings.max_chunks_per_session} chunks", "validation")
    missing = _status(session.live, total)["missing"]
    if missing:
        raise unprocessable(_missing_message(missing), "missing_input")
    sdir = storage.session_dir(request.app.state.settings, user.id, session.id)
    staged = stage_files(request, sdir, None, slides)
    commit_files(db, session, sdir, staged, live=True)
    session.live.total = total
    db.commit()
    job = enqueue(request, db, user, session, "finish", total)
    return {"job_id": job.id}
