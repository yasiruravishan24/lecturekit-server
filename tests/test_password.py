from tests.conftest import PASSWORD, login, new_user, register


def test_change_password(client):
    h = new_user(client)
    assert client.get("/auth/me", headers=h).json()["user"]["email_login"] is True
    r = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "a brand new secret"}, headers=h)
    assert r.status_code == 200 and r.json() == {"changed": True}
    assert client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).status_code == 401      # the old one is gone
    login(client, password="a brand new secret")


def test_the_current_password_must_be_right_and_the_new_one_different(client):
    h = new_user(client)
    bad = client.post("/auth/change-password", json={"current_password": "not it at all", "new_password": "a brand new secret"}, headers=h)
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "bad_current_password"
    same = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": PASSWORD}, headers=h)
    assert same.status_code == 422 and same.json()["error"]["code"] == "same_password"
    short = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "short"}, headers=h)
    assert short.status_code == 422
    assert client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "a brand new secret"}).status_code == 401      # not logged in


def test_repeated_wrong_current_passwords_are_blocked(client):
    h = new_user(client)
    for _ in range(client.app.state.settings.login_max_failures):
        client.post("/auth/change-password", json={"current_password": "wrong wrong wrong", "new_password": "a brand new secret"}, headers=h)
    r = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "a brand new secret"}, headers=h)
    assert r.status_code == 429
