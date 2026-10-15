"""Passwords (bcrypt), tokens (JWT HS256), the login lockout and the current_user dependency (SERVER_PLAN.md section 6)."""
import base64
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .errors import ApiError
from .models import User
from .settings import Settings

_bearer = HTTPBearer(auto_error=False)


def _prehash(password: str) -> bytes:
    """SHA-256 first so passwords of any length work (bcrypt itself only reads the first 72 bytes)."""
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode())
    except ValueError:
        return False


_dummy_hash: str | None = None


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check, so an unknown email is not faster than a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("dummy-password-for-timing", 4)
    verify_password(password, _dummy_hash)


def create_token(settings: Settings, user_id: int) -> tuple[str, datetime]:
    expires = datetime.now(timezone.utc) + timedelta(days=settings.token_days)
    token = jwt.encode({"sub": str(user_id), "exp": expires, "iat": datetime.now(timezone.utc)},
                       settings.jwt_secret, algorithm="HS256")
    return token, expires.replace(tzinfo=None)


def decode_token(settings: Settings, token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})


class LoginGuard:
    """After `max_failures` wrong logins for one email within `window` seconds: blocked for `window` seconds (in memory)."""

    def __init__(self, max_failures: int, window: int):
        self.max, self.window = max_failures, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked_for(self, email: str) -> int:
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(email, []) if now - t < self.window]
            self._fails[email] = fails
            if len(fails) >= self.max:
                return max(1, int(fails[-1] + self.window - now))
        return 0

    MAX_TRACKED = 10_000                      # emails remembered at once (stops memory growing under a flood of random emails)

    def record_failure(self, email: str) -> None:
        now = time.time()
        with self._lock:
            if len(self._fails) >= self.MAX_TRACKED:
                self._fails = {e: [t for t in ts if now - t < self.window] for e, ts in self._fails.items()}
                self._fails = {e: ts for e, ts in self._fails.items() if ts}
            self._fails.setdefault(email, []).append(now)

    def reset(self, email: str) -> None:
        with self._lock:
            self._fails.pop(email, None)


def current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    """Every route except /health needs a valid, unexpired token of an active user."""
    if cred is None:
        raise ApiError(401, "unauthorized", "Please log in")
    try:
        payload = decode_token(request.app.state.settings, cred.credentials)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        raise ApiError(401, "unauthorized", "Please log in") from None
    user = db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if user is None:
        raise ApiError(401, "unauthorized", "Please log in")
    request.state.user_id = user.id
    return user
