"""Upload a recording and slides, start processing, retry (SERVER_PLAN.md section 7)."""
from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.orm import Session

from lecturekit import api as lk
from lecturekit.slidefiles import SLIDE_TYPES      # a constant only: the types lecturekit can read

from .. import storage
from ..access import own_session
from ..db import get_db
from ..errors import conflict, unprocessable
from ..jobs import active, enqueue
from ..models import LectureSession, MediaFile, User
from ..security import current_user
from ..serialize import session_out

router = APIRouter(tags=["uploads"])


def stage_files(request: Request, sdir, recording: UploadFile | None, slides: list[UploadFile]):
    """Save uploads to temporary names first, so a bad file in the middle leaves nothing behind.

    Returns (staged, commit): staged is [(kind, original_name, tmp_path, final_ext, size)]."""
    settings = request.app.state.settings
    staged, updir = [], storage.upload_dir(sdir)
    try:
        if recording is not None and recording.filename:
            ext = storage.check_extension(recording.filename, lk.MEDIA_TYPES, "recording")
            tmp = updir / ".recording.part"
            size = storage.save_upload(recording, tmp, settings.max_recording_mb)
            staged.append(("recording", recording.filename, tmp, ext, size))
        for n, slide in enumerate([s for s in slides if s.filename], 1):
            ext = storage.check_extension(slide.filename, SLIDE_TYPES, "slides")
            tmp = updir / f".slide_{n:02d}.part"
            size = storage.save_upload(slide, tmp, settings.max_slides_mb)
            staged.append(("slides", slide.filename, tmp, ext, size))
    except BaseException:
        for _, _, tmp, _, _ in staged:
            storage.remove_file(tmp)
        raise
    return staged


def commit_files(db: Session, session: LectureSession, sdir, staged, live: bool = False) -> None:
    """Move staged files to their server-made names and record them. A new recording replaces the old one.

    live=True (slides sent when finishing a live recording): keep the chunks and the state as they are."""
    updir = storage.upload_dir(sdir)
    slide_no = sum(1 for f in session.files if f.kind == "slides")
    for kind, original, tmp, ext, size in staged:
        if kind == "recording":
            for old in [f for f in session.files if f.kind == "recording"]:
                storage.remove_file(updir / old.stored_name)
                session.files.remove(old)
            stored = f"recording{ext}"
        else:
            slide_no += 1
            stored = f"slide_{slide_no:02d}{ext}"
        tmp.replace(updir / stored)
        session.files.append(MediaFile(kind=kind, original_name=original[:255], stored_name=stored, bytes=size))
    session.has_slides = any(f.kind == "slides" for f in session.files)
    if staged and not live:
        storage.clear_derived(sdir)                      # nothing made from the old upload may be reused
        session.state = "uploaded" if any(f.kind == "recording" for f in session.files) else "empty"


@router.post("/sessions/{session_id}/upload")
def upload(session_id: int, request: Request, recording: UploadFile | None = File(None),
           slides: list[UploadFile] = File(default=[]), user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    session = own_session(db, user, session_id)
    if active(session):
        raise conflict("This lecture is being processed; wait until it has finished", "busy")
    if session.source == "live":
        raise conflict("This lecture is recorded live; send chunks instead", "live_session")
    sdir = storage.session_dir(request.app.state.settings, user.id, session.id)
    staged = stage_files(request, sdir, recording, slides)
    if not staged:
        raise unprocessable("Send a recording or slides", "missing_input")
    commit_files(db, session, sdir, staged)
    db.commit()
    return session_out(db, session)


def _start(request: Request, db: Session, user: User, session: LectureSession) -> dict:
    if active(session):
        raise conflict("This lecture is already queued or being processed", "busy")
    if session.source == "live":
        if session.live is None or session.live.total is None:
            raise conflict("Finish the live recording first", "not_finished")
        job = enqueue(request, db, user, session, "finish", session.live.total)
    else:
        if not any(f.kind == "recording" for f in session.files):
            raise unprocessable("Upload a recording first", "missing_input")
        job = enqueue(request, db, user, session, "process")
    return {"job_id": job.id}


@router.post("/sessions/{session_id}/process")
def process(session_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = own_session(db, user, session_id)
    if session.source == "live":
        raise conflict("This lecture is recorded live; finish the live upload instead", "live_session")
    return _start(request, db, user, session)


@router.post("/sessions/{session_id}/retry")
def retry(session_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = own_session(db, user, session_id)
    if session.state != "failed":
        raise conflict("Only a failed lecture can be retried", "not_failed")
    return _start(request, db, user, session)
