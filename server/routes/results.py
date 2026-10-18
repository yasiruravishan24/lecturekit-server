"""Status, notes, quiz and quiz attempts (SERVER_PLAN.md section 7).

Quiz answers and explanations leave the server only in the answer to a submitted attempt (rule 6)."""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..access import own_session
from ..db import get_db
from ..errors import conflict, not_found, unprocessable
from ..pdf_export import notes_pdf
from ..models import Attempt, AttemptAnswer, Job, User, iso
from ..schemas import AttemptIn
from ..validation import Id
from ..security import current_user
from ..serialize import job_out

router = APIRouter(tags=["results"])


def _done_session(db: Session, user: User, session_id: Id):
    session = own_session(db, user, session_id)
    if session.state != "done" or session.note is None:
        raise not_found()                                  # 404 until done
    return session


@router.get("/jobs/{job_id}")
def get_job(job_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if job is None:
        raise not_found()
    return job_out(job)


@router.get("/sessions/{session_id}/notes")
def get_notes(session_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = _done_session(db, user, session_id)
    n = session.note
    return {"title": n.title, "full_notes": json.loads(n.full_notes_json), "short_notes": json.loads(n.short_notes_json),
            "model": n.model, "config": session.config, "created_at": iso(n.created_at)}


@router.get("/sessions/{session_id}/quiz")
def get_quiz(session_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Questions and options only: no answer index, no explanation."""
    session = _done_session(db, user, session_id)
    return {"questions": [{"id": q.id, "position": q.position, "question": q.text,
                           "options": json.loads(q.options_json), "bloom_level": q.bloom_level, "source": q.source}
                          for q in session.questions]}


@router.post("/sessions/{session_id}/quiz/attempts", status_code=201)
def submit_attempt(session_id: Id, body: AttemptIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = _done_session(db, user, session_id)
    questions = list(session.questions)
    old_size = sum(1 for q in questions if q.source != "board")
    if len(body.answers) == old_size < len(questions):          # the page was opened before questions about the board were added
        raise conflict(f"Questions about the board were added to this quiz while you were answering ({len(questions) - old_size} new). Reload the quiz to answer them too", "quiz_changed")
    if len(body.answers) != len(questions):
        raise unprocessable(f"Send one answer for each of the {len(questions)} questions (null to skip one)")
    results, rows, score = [], [], 0
    for q, chosen in zip(questions, body.answers):
        options = json.loads(q.options_json)
        if chosen is not None and not 0 <= chosen < len(options):
            raise unprocessable(f"Question {q.position} has {len(options)} options; answer {chosen} is not one of them")
        correct = chosen == q.answer_index
        score += correct
        rows.append(AttemptAnswer(question_id=q.id, chosen_index=chosen, correct=correct))
        results.append({"question_id": q.id, "position": q.position, "chosen_index": chosen,
                        "correct_index": q.answer_index, "correct": correct, "explanation": q.explanation})
    attempt = Attempt(session_id=session.id, user_id=user.id, score=score, total=len(questions), answers=rows)
    db.add(attempt)
    db.commit()
    return {"attempt_id": attempt.id, "score": score, "total": len(questions), "results": results}


@router.get("/sessions/{session_id}/quiz/attempts")
def list_attempts(session_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """History: score and time only, never the answers."""
    session = _done_session(db, user, session_id)
    return [{"id": a.id, "score": a.score, "total": a.total, "submitted_at": iso(a.submitted_at)}
            for a in session.attempts]


@router.get("/sessions/{session_id}/export.md")
def export_markdown(session_id: Id, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = _done_session(db, user, session_id)
    sdir = storage.session_dir(request.app.state.settings, user.id, session.id)
    path = sdir / "out" / f"{session.config}.md"
    if not path.is_file():
        raise not_found()
    return FileResponse(path, media_type="text/markdown", filename=f"lecture-{session.id}.md")


@router.get("/sessions/{session_id}/export.pdf")
def export_pdf(session_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The notes, short notes and quiz (with answers) as a PDF made on the server."""
    session = _done_session(db, user, session_id)
    n = session.note
    questions = [{"question": q.text, "options": json.loads(q.options_json), "answer_index": q.answer_index, "explanation": q.explanation}
                 for q in session.questions]
    meta = f"Lecture notes - {n.created_at:%d %b %Y}"
    data = notes_pdf(n.title, meta, json.loads(n.full_notes_json), json.loads(n.short_notes_json), questions)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="lecture-{session.id}.pdf"'})
