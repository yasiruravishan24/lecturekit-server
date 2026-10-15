import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from server.main import create_app
from tests.conftest import INVITE, PASSWORD, login, make_settings, new_user, register


def test_register_needs_the_invite_code(client):
    r = client.post("/auth/register", json={"email": "a@example.com", "password": PASSWORD, "invite_code": "wrong"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invite_invalid"
    r = client.post("/auth/register", json={"email": "a@example.com", "password": PASSWORD, "invite_code": INVITE})
    assert r.status_code == 201
    assert r.json()["user"]["email"] == "a@example.com" and "password" not in r.text and "hash" not in r.text


def test_register_validation_and_duplicates(client):
    bad = [{"email": "not-an-email", "password": PASSWORD}, {"email": "b@example.com", "password": "short"}]
    for body in bad:
        r = client.post("/auth/register", json={**body, "invite_code": INVITE})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation"
    register(client, "Mixed.Case@Example.com")                       # stored lowercase
    r = client.post("/auth/register", json={"email": "mixed.case@example.com", "password": PASSWORD, "invite_code": INVITE})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    assert login(client, "MIXED.case@example.com")                    # login is case-insensitive too


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
