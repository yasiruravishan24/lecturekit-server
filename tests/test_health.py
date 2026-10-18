from server.main import create_app
from server.settings import Settings
import pytest


def test_health_is_public_and_has_no_secrets(client, root_client):
    r = root_client.get("/health")                      # also at the root, for monitoring
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "checks", "models", "whisper", "queue_length"}
    text = r.text
    assert "s" * 40 not in text and "ANTHROPIC_API_KEY" in body["checks"]
    assert client.get("/health").status_code == 200       # and under the API prefix
    assert "data_dir" not in body and "log_dir" not in body


def test_refuses_to_start_with_a_weak_secret(tmp_path):
    for bad in ("", "change-me", "short"):
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            create_app(Settings(data_dir=tmp_path, jwt_secret=bad, preload_whisper=False))


def test_unknown_route_and_error_shape(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json() == {"error": {"code": "not_found", "message": "Not found"}}
    r = client.get("/courses")                              # no token
    assert r.status_code == 401 and r.json()["error"]["message"] == "Please log in"


def test_api_docs_are_under_the_api_prefix(client, root_client):
    assert client.get("/docs").status_code == 200                       # /api/docs
    spec = client.get("/openapi.json")
    assert spec.status_code == 200 and "/api/courses" in spec.json()["paths"]
    assert root_client.get("/docs").status_code == 404                  # nothing outside /api


def test_root_explains_what_it_is_when_there_is_no_web_app(root_client):
    r = root_client.get("/")
    assert r.status_code == 200
    assert r.json() == {"name": "lecturekit server", "api": "/api", "docs": "/api/docs", "health": "/health", "web_app": "not installed"}


def test_the_web_app_is_served_at_root_when_it_exists(tmp_path, fake_lk):
    from fastapi.testclient import TestClient
    from tests.conftest import make_settings
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>web app</html>")
    with TestClient(create_app(make_settings(tmp_path, web_dist=dist))) as c:
        assert c.get("/").text == "<html>web app</html>"                 # the static app
        assert c.get("/api/health").status_code == 200                    # the API still answers under /api
        assert c.get("/health").status_code == 200


def test_the_web_app_is_served_with_normal_addresses(tmp_path):
    from fastapi.testclient import TestClient
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>the app</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    app = create_app(Settings(data_dir=tmp_path / "data", jwt_secret="s" * 40, preload_whisper=False, web_dist=dist))
    with TestClient(app) as c:
        for path in ("/", "/login", "/sessions/12", "/modules/3", "/quiz/5/result"):
            r = c.get(path)
            assert r.status_code == 200 and "the app" in r.text, path               # a page of the app: index.html
        assert c.get("/assets/app.js").text == "console.log(1)"                        # a real file
        assert c.get("/assets/missing.js").status_code == 404                          # a missing file is not index.html
        r = c.get("/api/nope")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"       # the API keeps its JSON 404
        assert c.get("/api/health").json()["ok"] is not None and c.get("/health").status_code == 200
        assert c.get("/api/docs").status_code == 200
