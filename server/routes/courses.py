"""Courses, modules and sessions: CRUD with ownership checks (SERVER_PLAN.md section 7)."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..access import own_course, own_module, own_session
from ..db import get_db
from ..errors import conflict
from ..jobs import active
from ..models import Course, LectureSession, Module, User, iso
from ..schemas import NameIn, SessionIn
from ..security import current_user
from ..serialize import session_out

router = APIRouter(tags=["courses"])


def _out(row, **extra) -> dict:
    return {"id": row.id, "name": row.name, "created_at": iso(row.created_at), **extra}


def _session_ids(sessions: list[LectureSession]) -> list[tuple[int, int]]:
    """(user_id, session_id) pairs, taken before the rows are deleted."""
    return [(s.user_id, s.id) for s in sessions]


def _delete_session_files(request: Request, ids: list[tuple[int, int]]) -> None:
    settings = request.app.state.settings
    for user_id, session_id in ids:
        storage.remove_tree(storage.session_dir(settings, user_id, session_id))


def _refuse_if_busy(sessions: list[LectureSession]) -> None:
    if any(active(s) for s in sessions):
        raise conflict("A lecture in here is still being processed; try again when it has finished", "busy")


# ---- courses -------------------------------------------------------------------------------------

@router.get("/courses")
def list_courses(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Course).where(Course.user_id == user.id).order_by(Course.id)).all()
    return [_out(c) for c in rows]


@router.post("/courses", status_code=201)
def create_course(body: NameIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    course = Course(user_id=user.id, name=body.name)
    db.add(course)
    db.commit()
    return _out(course)


@router.patch("/courses/{course_id}")
def rename_course(course_id: int, body: NameIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    course = own_course(db, user, course_id)
    course.name = body.name
    db.commit()
    return _out(course)


@router.delete("/courses/{course_id}", status_code=204)
def delete_course(course_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    course = own_course(db, user, course_id)
    sessions = [s for m in course.modules for s in m.sessions]
    _refuse_if_busy(sessions)
    ids = _session_ids(sessions)
    db.delete(course)
    db.commit()
    _delete_session_files(request, ids)
    return Response(status_code=204)


# ---- modules -------------------------------------------------------------------------------------

@router.get("/courses/{course_id}/modules")
def list_modules(course_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_out(m, course_id=m.course_id) for m in own_course(db, user, course_id).modules]


@router.post("/courses/{course_id}/modules", status_code=201)
def create_module(course_id: int, body: NameIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    course = own_course(db, user, course_id)
    module = Module(course_id=course.id, name=body.name)
    db.add(module)
    db.commit()
    return _out(module, course_id=course.id)


@router.patch("/modules/{module_id}")
def rename_module(module_id: int, body: NameIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    module = own_module(db, user, module_id)
    module.name = body.name
    db.commit()
    return _out(module, course_id=module.course_id)


@router.delete("/modules/{module_id}", status_code=204)
def delete_module(module_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    module = own_module(db, user, module_id)
    sessions = list(module.sessions)
    _refuse_if_busy(sessions)
    ids = _session_ids(sessions)
    db.delete(module)
    db.commit()
    _delete_session_files(request, ids)
    return Response(status_code=204)


# ---- sessions ------------------------------------------------------------------------------------

@router.get("/modules/{module_id}/sessions")
def list_sessions(module_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    module = own_module(db, user, module_id)
    return [session_out(db, s) for s in module.sessions]


@router.post("/modules/{module_id}/sessions", status_code=201)
def create_session(module_id: int, body: SessionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    module = own_module(db, user, module_id)
    session = LectureSession(module_id=module.id, user_id=user.id, title=body.title)
    db.add(session)
    db.commit()
    return session_out(db, session)


@router.get("/sessions/{session_id}")
def get_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return session_out(db, own_session(db, user, session_id))


@router.patch("/sessions/{session_id}")
def rename_session(session_id: int, body: SessionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = own_session(db, user, session_id)
    session.title = body.title
    db.commit()
    return session_out(db, session)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = own_session(db, user, session_id)
    _refuse_if_busy([session])
    ids = _session_ids([session])
    db.delete(session)
    db.commit()
    _delete_session_files(request, ids)
    return Response(status_code=204)
