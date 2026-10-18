"""Accounts: sign-up with email verification, login, Google login (SERVER_PLAN.md section 6)."""

import hmac
import time
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..db import get_db
from ..errors import ApiError
from ..google import verify_google_token
from ..mail_templates import verification_email, welcome_email
from ..models import Job, User, iso, utcnow
from ..schemas import EmailIn, GoogleIn, LoginIn, RegisterIn, VerifyIn
from ..security import (burn_password_check, create_token, current_user, hash_code, hash_password, new_code,
                        verify_password)
from ..serialize import user_out

router = APIRouter(prefix="/auth", tags=["auth"])


def _send_code(request: Request, user: User) -> None:
    """Make a new 6-digit code for the user (the old one stops working) and queue the email."""
    s = request.app.state.settings
    code = new_code()
    user.otp_hash = hash_code(s, user.email, code)
    user.otp_expires_at = utcnow() + timedelta(minutes=s.otp_minutes)
    user.otp_attempts = 0
    request.state.mail_jobs.append((user.email, *verification_email(user.full_name, code, s.otp_minutes)))
    request.app.state.resend_times[user.email] = time.time()


def _flush_mail(request: Request, background: BackgroundTasks) -> None:
    for to, subject, text, html in request.state.mail_jobs:
        background.add_task(request.app.state.mailer.send, to, subject, text, html)


@router.get("/config")
def config(request: Request):
    """What a client needs before the user logs in."""
    s = request.app.state.settings
    return {"google_enabled": bool(s.google_client_id), "google_client_id": s.google_client_id or None,
            "limits": {"daily_limit": s.daily_limit, "keep_media_days": s.keep_media_days, "max_recording_mb": s.max_recording_mb,
                       "max_slides_mb": s.max_slides_mb, "max_recording_parts": s.max_recording_parts}}


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    request.state.mail_jobs = []
    user = db.scalar(select(User).where(User.email == body.email))
    if user is not None and (user.email_verified or user.google_sub):
        raise ApiError(409, "email_taken", "That email is already registered")
    if user is None:
        user = User(email=body.email)
        db.add(user)
    # an address nobody has confirmed yet can be registered again: whoever owns the mailbox finishes it
    user.full_name = body.full_name
    user.password_hash = hash_password(body.password, settings.bcrypt_rounds)
    user.terms_accepted_at = utcnow()
    _send_code(request, user)
    db.commit()
    _flush_mail(request, background)
    return {"user": user_out(user), "verification_sent": True}


@router.post("/verify-email")
def verify_email(body: VerifyIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Confirm the email with the 6-digit code from the email (then the welcome email is sent). Five wrong tries cancel the code."""
    s = request.app.state.settings
    request.state.mail_jobs = []
    user = db.scalar(select(User).where(User.email == body.email, User.is_active.is_(True)))
    bad = ApiError(400, "invalid_code", "That code is not right")
    if user is None:
        raise bad
    if user.email_verified:
        return {"user": user_out(user)}
    if not user.otp_hash or user.otp_expires_at is None or user.otp_expires_at < utcnow():
        raise ApiError(400, "code_expired", "That code has expired; ask for a new one")
    if user.otp_attempts >= s.otp_max_attempts:
        raise ApiError(429, "too_many_attempts", "Too many wrong codes; ask for a new one")
    if not hmac.compare_digest(user.otp_hash, hash_code(s, user.email, body.code)):
        user.otp_attempts += 1
        db.commit()
        raise bad
    user.email_verified, user.otp_hash, user.otp_expires_at, user.otp_attempts = True, None, None, 0
    db.commit()
    request.state.mail_jobs.append((user.email, *welcome_email(user.full_name, s.web_origin, s.daily_limit, s.keep_media_days)))
    _flush_mail(request, background)
    return {"user": user_out(user)}


@router.post("/resend-verification")
def resend_verification(body: EmailIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Always answers the same, so it cannot be used to find out which emails have an account."""
    settings, asked = request.app.state.settings, request.app.state.resend_times
    request.state.mail_jobs = []
    user = db.scalar(select(User).where(User.email == body.email, User.is_active.is_(True)))
    now = time.time()
    if user and not user.email_verified and user.password_hash and now - asked.get(user.email, 0) >= settings.resend_seconds:
        _send_code(request, user)
        db.commit()
        _flush_mail(request, background)
    return {"sent": True}


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
    if not user.email_verified:
        raise ApiError(403, "email_not_verified", "Confirm your email first: enter the code we sent you")
    token, expires = create_token(settings, user.id)
    return {"token": token, "expires_at": iso(expires), "user": user_out(user)}


@router.post("/google")
def google_login(body: GoogleIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Log in (or sign up) with a Google ID token. A Google account counts as a verified email."""
    settings = request.app.state.settings
    request.state.mail_jobs = []
    if not settings.google_client_id:
        raise ApiError(503, "google_disabled", "Google sign-in is not set up on this server")
    info = verify_google_token(settings.google_client_id, body.id_token)
    info["email"] = info["email"].strip().lower()
    user = db.scalar(select(User).where(User.google_sub == info["sub"]))
    if user is None:
        user = db.scalar(select(User).where(User.email == info["email"]))
        if user is None and not body.accept_terms:
            raise ApiError(422, "terms_required", "Accept the terms to create your account")
        if user is None:
            user = User(email=info["email"], terms_accepted_at=utcnow(), full_name=info["name"])
            db.add(user)
        first_time = user.email_verified is not True   # a new account, or one whose email was never confirmed
        user.google_sub = info["sub"]                   # an existing account with this email is linked
        user.email_verified = True
        if not user.full_name:
            user.full_name = info["name"]
        db.commit()
        if first_time:
            request.state.mail_jobs.append((user.email, *welcome_email(user.full_name, settings.web_origin, settings.daily_limit, settings.keep_media_days)))
            _flush_mail(request, background)
    if not user.is_active:
        raise ApiError(401, "bad_credentials", "This account is not available")
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
