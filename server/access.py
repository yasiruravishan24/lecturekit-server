"""Ownership checks (SERVER_PLAN.md rule 7): every object is loaded through the logged-in user.

A wrong id and someone else's id look the same: 404."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import not_found
from .models import Course, LectureSession, Module, User


def own_course(db: Session, user: User, course_id: int) -> Course:
    row = db.scalar(select(Course).where(Course.id == course_id, Course.user_id == user.id))
    if row is None:
        raise not_found()
    return row


def own_module(db: Session, user: User, module_id: int) -> Module:
    row = db.scalar(select(Module).join(Course, Module.course_id == Course.id)
                    .where(Module.id == module_id, Course.user_id == user.id))
    if row is None:
        raise not_found()
    return row


def own_session(db: Session, user: User, session_id: int) -> LectureSession:
    row = db.scalar(select(LectureSession).where(LectureSession.id == session_id, LectureSession.user_id == user.id))
    if row is None:
        raise not_found()
    return row
