"""The list of notifications (the live ones arrive on the WebSocket, events.py)."""
from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import not_found
from ..models import Notification, User
from ..notifications import out
from ..security import current_user
from ..validation import Id

router = APIRouter(tags=["notifications"])


@router.get("/notifications")
def list_notifications(limit: int = Query(30, ge=1, le=50), user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(limit)).all()
    unread = db.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.read.is_(False))) or 0
    return {"items": [out(n) for n in rows], "unread": unread}


@router.post("/notifications/read-all", status_code=204)
def read_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(update(Notification).where(Notification.user_id == user.id).values(read=True))
    db.commit()
    return Response(status_code=204)


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_one(notification_id: Id, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id))
    if row is None:
        raise not_found()
    row.read = True
    db.commit()
    return Response(status_code=204)


@router.delete("/notifications", status_code=204)
def clear(user: User = Depends(current_user), db: Session = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id)).all():
        db.delete(n)
    db.commit()
    return Response(status_code=204)
