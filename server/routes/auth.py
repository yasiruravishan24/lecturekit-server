"""Accounts (SERVER_PLAN.md section 6)."""
import hmac

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..db import get_db
from ..errors import ApiError
from ..models import Job, User, iso
from ..schemas import LoginIn, RegisterIn
from ..security import (burn_password_check, create_token, current_user, hash_password, verify_password)
from ..serialize import user_out

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    if not hmac.compare_digest(body.invite_code.encode(), settings.invite_code.encode()):
        raise ApiError(403, "invite_invalid", "Invalid invite code")
    if db.scalar(select(User).where(User.email == body.email)):
        raise ApiError(409, "email_taken", "That email is already registered")
    user = User(email=body.email, password_hash=hash_password(body.password, settings.bcrypt_rounds))
    db.add(user)
    db.commit()
    return {"user": user_out(user)}


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    settings, guard = request.app.state.settings, request.app.state.login_guard
    wait = guard.blocked_for(body.email)
    if wait:
        raise ApiError(429, "too_many_attempts", f"Too many failed logins; try again in {max(1, wait // 60)} minutes")
    user = db.scalar(select(User).where(User.email == body.email, User.is_active.is_(True)))
    if user is None:
        burn_password_check(body.password)
    if user is None or not verify_password(body.password, user.password_hash):
        guard.record_failure(body.email)
        raise ApiError(401, "bad_credentials", "Wrong email or password")        # same answer for both cases
    guard.reset(body.email)
    token, expires = create_token(settings, user.id)
    return {"token": token, "expires_at": iso(expires), "user": user_out(user)}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"user": user_out(user)}


@router.delete("/me", status_code=204)
def delete_me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Delete the account and everything it owns: rows (cascade) and files."""
    busy = db.scalar(select(Job.id).where(Job.user_id == user.id, Job.state.in_(("queued", "processing"))).limit(1))
    if busy:
        raise ApiError(409, "busy", "A lecture is still being processed; try again when it has finished")
    settings, user_id = request.app.state.settings, user.id
    db.delete(user)
    db.commit()
    storage.remove_tree(settings.users_dir / str(user_id))
    return Response(status_code=204)
