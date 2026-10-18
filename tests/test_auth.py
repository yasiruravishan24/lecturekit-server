import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from server.main import create_app
from tests.conftest import PASSWORD, login, make_settings, new_user, register, register_body


def test_register_sends_a_code_by_email_and_logs_nobody_in(client):
    r = client.post("/auth/register", json=register_body())
    assert r.status_code == 201
    body = r.json()
    assert body["verification_sent"] is True and "token" not in body and "code" not in r.text
    assert body["user"]["email"] == "a@example.com" and body["user"]["full_name"] == "Test User"
    assert body["user"]["email_verified"] is False and "password" not in r.text and "hash" not in r.text
    (to, subject, text, html), = client.app.state.mailer.sent
    code = client.app.state.mailer.code_for("a@example.com")
    assert to == "a@example.com" and subject == "Verify your LectureKit email address" and code not in subject
    assert len(code) == 6 and "Hi Test User" in text and "10 minutes" in text and "http" not in text
    assert html.startswith("<!doctype html>") and code in html.replace("&nbsp;", "") and "Test User" in html


def test_the_welcome_email_is_sent_once_the_email_is_verified(client):
    register(client, verify=False)
    mails = client.app.state.mailer.sent
    assert len(mails) == 1                                            # only the code so far
    client.post("/auth/verify-email", json={"email": "a@example.com", "code": client.app.state.mailer.code_for("a@example.com")})
    assert len(mails) == 2
    to, subject, text, html = mails[1]
    assert to == "a@example.com" and subject == "Welcome to LectureKit, Test!" and "Hi Test," in text and "<html" in html
    client.post("/auth/verify-email", json={"email": "a@example.com", "code": "000000"})        # confirmed already: no second welcome
    assert len(mails) == 2


def test_emails_escape_the_name():
    from server.mail_templates import verification_email, welcome_email
    evil = "<script>alert(1)</script>"
    assert "<script>" not in verification_email(evil, "123456", 10)[2] and "<script>" not in welcome_email(evil)[2]
    assert "&lt;script&gt;" in welcome_email(evil)[2]


def test_register_validation_and_duplicates(client):
    bad = [register_body(email="not-an-email"), register_body(email="b@example.com", password="short"),
           register_body(email="b@example.com", accept_terms=False), register_body(email="b@example.com", full_name="  "),
           {"email": "b@example.com", "password": PASSWORD}]
    for body in bad:
        r = client.post("/auth/register", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation", body
    register(client, "Mixed.Case@Example.com")                       # stored lowercase
    r = client.post("/auth/register", json=register_body("mixed.case@example.com"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    assert login(client, "MIXED.case@example.com")                    # login is case-insensitive too


def test_login_needs_a_verified_email(client):
    register(client, verify=False)
    r = client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "email_not_verified"
    wrong = client.post("/auth/login", json={"email": "a@example.com", "password": "not the password"})
    assert wrong.status_code == 401                                   # a wrong password is not told about verification
    r = client.post("/auth/verify-email", json={"email": "A@example.com", "code": client.app.state.mailer.code_for("a@example.com")})
    assert r.status_code == 200 and r.json()["user"]["email_verified"] is True
    assert login(client)
    again = client.post("/auth/verify-email", json={"email": "a@example.com", "code": "000000"})
    assert again.status_code == 200                                   # already confirmed: nothing to do


def test_wrong_codes_are_refused_and_five_of_them_cancel_the_code(client):
    register(client, verify=False)
    good = client.app.state.mailer.code_for("a@example.com")
    wrong = "000000" if good != "000000" else "111111"
    r = client.post("/auth/verify-email", json={"email": "a@example.com", "code": wrong})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_code"
    assert client.post("/auth/verify-email", json={"email": "nobody@example.com", "code": wrong}).json()["error"]["code"] == "invalid_code"
    for _ in range(4):
        client.post("/auth/verify-email", json={"email": "a@example.com", "code": wrong})
    r = client.post("/auth/verify-email", json={"email": "a@example.com", "code": good})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_attempts"      # even the right code, until a new one is sent
    client.app.state.resend_times.clear()
    client.app.state.settings.__dict__["resend_seconds"] = 0
    client.post("/auth/resend-verification", json={"email": "a@example.com"})
    fresh = client.app.state.mailer.code_for("a@example.com")
    assert client.post("/auth/verify-email", json={"email": "a@example.com", "code": fresh}).status_code == 200


def test_an_expired_code_is_refused(client):
    from server.models import User
    from sqlalchemy import update
    register(client, verify=False)
    with client.app.state.session_factory() as db:
        db.execute(update(User).values(otp_expires_at=datetime(2020, 1, 1)))
        db.commit()
    r = client.post("/auth/verify-email", json={"email": "a@example.com", "code": client.app.state.mailer.code_for("a@example.com")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "code_expired"


def test_the_code_is_stored_hashed(client):
    from server.models import User
    from sqlalchemy import select
    register(client, verify=False)
    code = client.app.state.mailer.code_for("a@example.com")
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User))
        assert user.otp_hash and code not in user.otp_hash and len(user.otp_hash) == 64


def test_resend_verification(client):
    register(client, verify=False)
    mails = client.app.state.mailer.sent
    assert client.post("/auth/resend-verification", json={"email": "a@example.com"}).json() == {"sent": True}
    assert len(mails) == 1                                            # too soon after the first one (60 s)
    client.app.state.resend_times.clear()
    client.app.state.settings.__dict__["resend_seconds"] = 0
    r = client.post("/auth/resend-verification", json={"email": "a@example.com"})
    assert r.status_code == 200 and len(mails) == 2
    r = client.post("/auth/resend-verification", json={"email": "nobody@example.com"})
    assert r.status_code == 200 and r.json() == {"sent": True} and len(mails) == 2    # same answer, nothing sent


def test_an_unconfirmed_address_can_be_registered_again(client):
    client.post("/auth/register", json=register_body(password="first password 1"))
    r = client.post("/auth/register", json=register_body(password="second password 2", full_name="Real Owner"))
    assert r.status_code == 201 and r.json()["user"]["full_name"] == "Real Owner"
    client.post("/auth/verify-email", json={"email": "a@example.com", "code": client.app.state.mailer.code_for("a@example.com")})
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "first password 1"}).status_code == 401
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "second password 2"}).status_code == 200


def test_config_and_google_login(client, monkeypatch):
    cfg = client.get("/auth/config").json()
    assert cfg["google_enabled"] is False and cfg["google_client_id"] is None
    assert cfg["limits"] == {"daily_limit": 5, "keep_media_days": 7, "max_recording_mb": 1500, "max_slides_mb": 100, "max_recording_parts": 10}
    assert client.post("/auth/google", json={"id_token": "x", "accept_terms": True}).json()["error"]["code"] == "google_disabled"
    client.app.state.settings.__dict__["google_client_id"] = "client-123.apps.googleusercontent.com"
    assert client.get("/auth/config").json()["google_enabled"] is True
    info = {"sub": "g-1", "email": "Student@Example.com", "name": "Student One"}
    monkeypatch.setattr("server.routes.auth.verify_google_token", lambda client_id, token: info)
    r = client.post("/auth/google", json={"id_token": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "terms_required"
    r = client.post("/auth/google", json={"id_token": "x", "accept_terms": True})
    body = r.json()
    assert r.status_code == 200 and body["user"]["email"] == "student@example.com" and body["user"]["email_verified"] is True
    assert [m[1] for m in client.app.state.mailer.sent] == ["Welcome to LectureKit, Student!"]
    assert body["user"]["full_name"] == "Student One" and body["user"]["google_linked"] is True
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"}).status_code == 200
    again = client.post("/auth/google", json={"id_token": "x"})                    # second time: no terms needed
    assert again.status_code == 200 and again.json()["user"]["id"] == body["user"]["id"]
    assert len(client.app.state.mailer.sent) == 1                                   # no second welcome
    # a Google-only account has no password to guess
    assert client.post("/auth/login", json={"email": "student@example.com", "password": ""}).status_code == 401
    assert client.post("/auth/login", json={"email": "student@example.com", "password": PASSWORD}).status_code == 401


def test_google_links_an_existing_password_account(client, monkeypatch):
    register(client, "a@example.com")
    client.app.state.settings.__dict__["google_client_id"] = "id"
    monkeypatch.setattr("server.routes.auth.verify_google_token", lambda c, t: {"sub": "g-9", "email": "a@example.com", "name": "Other Name"})
    r = client.post("/auth/google", json={"id_token": "x"})
    assert r.status_code == 200 and r.json()["user"]["full_name"] == "Test User" and r.json()["user"]["google_linked"] is True
    assert login(client)                                                           # the password still works


def test_google_token_checks(monkeypatch):
    import httpx
    from server.errors import ApiError
    from server.google import verify_google_token

    def fake(status, data):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(status, json=data, request=httpx.Request("GET", "http://x")))

    good = {"aud": "cid", "iss": "https://accounts.google.com", "sub": "1", "email": "a@b.co", "email_verified": "true", "name": "A"}
    fake(200, good)
    assert verify_google_token("cid", "t")["email"] == "a@b.co"
    for bad in ({**good, "aud": "other"}, {**good, "iss": "evil.com"}, {**good, "email_verified": "false"}, {}):
        fake(200, bad)
        try:
            verify_google_token("cid", "t")
            raise AssertionError("accepted a bad token")
        except ApiError as e:
            assert e.status == 401 and e.code == "google_invalid"
    fake(400, {"error": "invalid_token"})
    try:
        verify_google_token("cid", "t")
        raise AssertionError("accepted a bad token")
    except ApiError as e:
        assert e.code == "google_invalid"


def test_an_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    from server.db import make_session_factory
    (tmp_path / "data").mkdir()
    con = sqlite3.connect(tmp_path / "data" / "server.db")
    con.execute('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR(254) UNIQUE, password_hash VARCHAR(100), created_at DATETIME, is_active BOOLEAN)')
    con.execute("INSERT INTO \"user\" VALUES (1, 'old@example.com', 'x', '2026-01-01', 1)")
    con.commit(); con.close()
    sf = make_session_factory(make_settings(tmp_path))
    with sf() as db:
        from server.models import User
        old = db.get(User, 1)
        assert old.email_verified is True and old.full_name == "" and old.google_sub is None and old.otp_hash is None and old.otp_attempts == 0


def test_login_and_me(client):
    register(client)
    r = client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    body = r.json()
    assert r.status_code == 200 and body["token"] and body["expires_at"].endswith("Z") and body["user"]["email"] == "a@example.com"
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["user"]["email"] == "a@example.com"


def test_wrong_password_and_unknown_email_look_the_same(client):
    register(client)
    wrong = client.post("/auth/login", json={"email": "a@example.com", "password": "not the password"})
    unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_bad_tokens_give_401(client, settings):
    user = register(client)
    now = datetime.now(timezone.utc)
    cases = {
        "expired": jwt.encode({"sub": str(user["id"]), "exp": now - timedelta(seconds=5)}, settings.jwt_secret, "HS256"),
        "other secret": jwt.encode({"sub": str(user["id"]), "exp": now + timedelta(days=1)}, "x" * 40, "HS256"),
        "no sub": jwt.encode({"exp": now + timedelta(days=1)}, settings.jwt_secret, "HS256"),
        "unknown user": jwt.encode({"sub": "999", "exp": now + timedelta(days=1)}, settings.jwt_secret, "HS256"),
        "garbage": "not.a.token",
    }
    good = login(client)["Authorization"].split()[1]
    cases["tampered"] = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    for name, token in cases.items():
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, name
        assert r.json()["error"]["message"] == "Please log in", name
    assert client.get("/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_lockout_after_five_failures(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, login_window_seconds=2))
    with TestClient(app, base_url="http://testserver/api") as c:
        register(c)
        for _ in range(5):
            assert c.post("/auth/login", json={"email": "a@example.com", "password": "wrong password"}).status_code == 401
        r = c.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})      # even the right one
        assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_attempts"
        assert c.post("/auth/login", json={"email": "other@example.com", "password": "x"}).status_code == 401   # other emails unaffected
        time.sleep(2.2)
        assert c.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).status_code == 200


def test_successful_login_resets_the_counter(client):
    register(client)
    for _ in range(4):
        client.post("/auth/login", json={"email": "a@example.com", "password": "wrong password"})
    assert client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).status_code == 200
    for _ in range(4):
        assert client.post("/auth/login", json={"email": "a@example.com", "password": "wrong password"}).status_code == 401


def test_delete_account_removes_rows_and_files(client, settings):
    h = new_user(client)
    c = client.post("/courses", json={"name": "CS"}, headers=h).json()
    m = client.post(f"/courses/{c['id']}/modules", json={"name": "W"}, headers=h).json()
    s = client.post(f"/modules/{m['id']}/sessions", json={"title": "L"}, headers=h).json()
    me = client.get("/auth/me", headers=h).json()["user"]["id"]
    folder = settings.users_dir / str(me) / "sessions" / str(s["id"])
    folder.mkdir(parents=True)
    (folder / "transcript.json").write_text("{}")
    assert client.delete("/auth/me", headers=h).status_code == 204
    assert not (settings.users_dir / str(me)).exists()
    assert client.get("/auth/me", headers=h).status_code == 401                    # the token no longer works
    assert client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).status_code == 401
    h2 = new_user(client, "b@example.com")                                         # and the email can be used again
    assert client.get("/courses", headers=h2).json() == []


def test_login_guard_memory_is_bounded():
    from server.security import LoginGuard
    guard = LoginGuard(5, 1)
    guard.MAX_TRACKED = 50
    for i in range(500):
        guard.record_failure(f"user{i}@example.com")
    assert len(guard._fails) <= 500
    time.sleep(1.1)                                    # everything is older than the window now
    guard.record_failure("one-more@example.com")
    assert len(guard._fails) <= 2                      # old entries were dropped


def test_the_welcome_email_has_a_button_only_when_the_app_address_is_known():
    from server.mail_templates import welcome_email
    assert "Open LectureKit" not in welcome_email("Ann Lee", "")[2]
    subject, text, html = welcome_email("Ann Lee", "https://app.example.com", daily_limit=3, keep_days=14)
    assert subject == "Welcome to LectureKit, Ann!" and 'href="https://app.example.com"' in html
    assert "up to 3 lectures per day" in text and "after 14 days" in text
