from server.google import verify_google_token


def _token(monkeypatch, aud):
    class R:
        status_code = 200
        def json(self):
            return {"aud": aud, "iss": "https://accounts.google.com", "sub": "1", "email": "a@b.co", "email_verified": "true", "name": "A"}
    monkeypatch.setattr("server.google.httpx.get", lambda *a, **k: R())


def test_a_token_for_the_web_or_the_iphone_client_is_accepted_and_others_are_not(monkeypatch):
    ids = ["web.apps.googleusercontent.com", "ios.apps.googleusercontent.com"]
    for aud in ids:
        _token(monkeypatch, aud)
        assert verify_google_token(ids, "t")["email"] == "a@b.co"
    _token(monkeypatch, "someone-else.apps.googleusercontent.com")
    try:
        verify_google_token(ids, "t")
        raise AssertionError("a token for another app must be refused")
    except Exception as e:
        assert getattr(e, "status_code", 401) == 401


def test_the_config_tells_the_phone_its_client_id(client):
    assert client.get("/auth/config").json()["google_ios_client_id"] is None
    client.app.state.settings.__dict__["google_ios_client_id"] = "ios.apps.googleusercontent.com"
    cfg = client.get("/auth/config").json()
    assert cfg["google_ios_client_id"] == "ios.apps.googleusercontent.com" and cfg["google_enabled"] is False      # the web button stays off without a web client


def test_google_login_works_with_only_the_iphone_client(client, monkeypatch):
    client.app.state.settings.__dict__["google_ios_client_id"] = "ios.apps.googleusercontent.com"
    seen = {}
    def fake(ids, token):
        seen["ids"] = ids
        return {"sub": "g-1", "email": "phone@example.com", "name": "Phone User"}
    monkeypatch.setattr("server.routes.auth.verify_google_token", fake)
    r = client.post("/auth/google", json={"id_token": "x", "accept_terms": True})
    assert r.status_code == 200 and seen["ids"] == ["ios.apps.googleusercontent.com"]
