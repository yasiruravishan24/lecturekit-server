from server.main import create_app
from server.settings import Settings
import pytest


def test_health_is_public_and_has_no_secrets(client, root_client):
    r = root_client.get("/health")                      # also at the root, for monitoring
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "checks", "models", "whisper", "queue_length"}
    text = r.text
    assert "s" * 40 not in text and "invite-123" not in text and "ANTHROPIC_API_KEY" in body["checks"]
    assert client.get("/health").status_code == 200       # and under the API prefix
    assert "data_dir" not in body and "log_dir" not in body


def test_refuses_to_start_with_a_weak_secret(tmp_path):
    for bad in ("", "change-me", "short"):
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            create_app(Settings(data_dir=tmp_path, jwt_secret=bad, invite_code="x", preload_whisper=False))
    with pytest.raises(RuntimeError, match="INVITE_CODE"):
        create_app(Settings(data_dir=tmp_path, jwt_secret="s" * 40, invite_code="", preload_whisper=False))


def test_unknown_route_and_error_shape(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json() == {"error": {"code": "not_found", "message": "Not found"}}
    r = client.get("/courses")                              # no token
    assert r.status_code == 401 and r.json()["error"]["message"] == "Please log in"
