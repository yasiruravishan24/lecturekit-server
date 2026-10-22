"""Checking a Google sign-in token (the ID token the browser or the extension gets from Google)."""
import httpx

from .errors import ApiError

TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def verify_google_token(client_id: str | list[str] | tuple[str, ...], id_token: str) -> dict:
    """Returns {sub, email, name} for a valid token made for this app (one of its client IDs: the web one, the iPhone one), otherwise raises a 401."""
    allowed = {client_id} if isinstance(client_id, str) else set(client_id)
    try:
        r = httpx.get(TOKENINFO, params={"id_token": id_token}, timeout=10)
    except httpx.HTTPError:
        raise ApiError(503, "google_unavailable", "Could not reach Google; try again in a moment") from None
    info = r.json() if r.status_code == 200 else {}
    ok = (info.get("aud") in allowed and info.get("iss") in ISSUERS and info.get("sub") and info.get("email")
          and str(info.get("email_verified")).lower() == "true")
    if not ok:
        raise ApiError(401, "google_invalid", "Google sign-in failed; try again")
    return {"sub": info["sub"], "email": info["email"].strip().lower(), "name": (info.get("name") or "").strip()[:120]}
