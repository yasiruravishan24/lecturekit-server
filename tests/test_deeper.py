"""Go deeper: made on request, in the background lane."""
import time

import pytest
from fastapi.testclient import TestClient
from lecturekit.errors import LLMError

from server.main import create_app
from tests.conftest import file_part, make_session, make_settings, new_user, wait_job
from tests.test_background import process


@pytest.fixture
def client(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, visuals_enabled=False))
    with TestClient(app, base_url="http://testserver/api") as c:
        c.app_ = app
        yield c


def wait_deeper(client, h, sid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/sessions/{sid}/deeper", headers=h).json()
        if d["state"] in ("done", "failed"):
            return d
        time.sleep(0.02)
    raise AssertionError(d)


def test_go_deeper_is_made_on_request_and_adds_extra_questions(client, fake_lk):
    h = new_user(client)
    sid = process(client, h)
    assert client.get(f"/sessions/{sid}/deeper", headers=h).json() == {"state": "none", "percent": None, "stage": None, "data": None}
    job = client.post(f"/sessions/{sid}/deeper", headers=h).json()["job_id"]
    d = wait_deeper(client, h, sid)
    assert d["state"] == "done" and d["data"]["web"] is True and d["data"]["topics"][0]["links"][0]["url"] == "https://docs.python.org/"
    assert wait_job(client, h, job)["state"] == "done"
    call = next(c for c in fake_lk.calls if c[0] == "deeper")
    assert call[3:] == (3, 4)                                                   # DEEPER_QUESTIONS and DEEPER_SEARCHES of the settings
    quiz = client.get(f"/sessions/{sid}/quiz", headers=h).json()["questions"]
    assert [(q["position"], q["source"]) for q in quiz][-1] == (len(quiz), "extra")                 # after the old ones
    assert "answer_index" not in str(quiz[-1])                                  # the answer is still hidden
    assert client.get(f"/sessions/{sid}", headers=h).json()["deeper_state"] == "done"
    assert client.post(f"/sessions/{sid}/deeper", headers=h).status_code == 409           # once is enough


def test_it_needs_the_notes_and_another_user_cannot_see_it(client):
    h = new_user(client, "a@example.com")
    sid = make_session(client, h)
    assert client.post(f"/sessions/{sid}/deeper", headers=h).status_code == 422             # no notes yet
    done = process(client, h)
    other = new_user(client, "b@example.com")
    assert client.get(f"/sessions/{done}/deeper", headers=other).status_code == 404
    assert client.post(f"/sessions/{done}/deeper", headers=other).status_code == 404


def test_a_failure_keeps_the_notes_and_can_be_tried_again(client, fake_lk):
    h = new_user(client)
    sid = process(client, h)
    fake_lk.bg.deeper_fail = LLMError("credit")
    client.post(f"/sessions/{sid}/deeper", headers=h)
    assert wait_deeper(client, h, sid)["state"] == "failed"
    assert client.get(f"/sessions/{sid}/notes", headers=h).status_code == 200
    fake_lk.bg.deeper_fail = None
    client.post(f"/sessions/{sid}/deeper", headers=h)
    assert wait_deeper(client, h, sid)["state"] == "done"
    kinds = [n["kind"] for n in client.get("/notifications", headers=h).json()["items"]]
    assert "deeper_failed" in kinds and "deeper_ready" in kinds


def test_new_notes_forget_the_old_go_deeper(client):
    h = new_user(client)
    sid = process(client, h)
    client.post(f"/sessions/{sid}/deeper", headers=h)
    wait_deeper(client, h, sid)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("again.mp4"))])          # a new recording replaces everything
    assert client.get(f"/sessions/{sid}", headers=h).json()["deeper_state"] == "none"


def test_by_itself_right_after_the_notes_at_the_same_time_as_the_pictures(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, visuals_enabled=True, deeper_auto=True))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        fake_lk.bg.gate.clear()                                    # hold the picture lane: "learn more" must not wait for it
        sid = process(c, h)
        end = time.time() + 5
        while time.time() < end and c.get(f"/sessions/{sid}", headers=h).json()["deeper_state"] != "processing":
            time.sleep(0.02)
        s = c.get(f"/sessions/{sid}", headers=h).json()
        assert s["visuals_state"] == "processing" and s["deeper_state"] == "processing"           # both are being made at once
        fake_lk.bg.gate.set()
        end = time.time() + 10
        while time.time() < end:
            s = c.get(f"/sessions/{sid}", headers=h).json()
            if s["deeper_state"] == "done" and s["replay_state"] == "ready":
                break
            time.sleep(0.02)
        assert s["deeper_state"] == "done" and s["replay_state"] == "ready" and s["visuals_state"] == "done"
        quiz = c.get(f"/sessions/{sid}/quiz", headers=h).json()["questions"]
        assert [q["position"] for q in quiz] == list(range(1, len(quiz) + 1))                   # no two questions with the same number
        assert {"board", "extra"} <= {q["source"] for q in quiz}


def test_a_failing_learn_more_does_not_stop_the_pictures_or_the_replay(tmp_path, fake_lk):
    fake_lk.bg.deeper_fail = LLMError("credit")
    app = create_app(make_settings(tmp_path, visuals_enabled=True, deeper_auto=True))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = process(c, h)
        end = time.time() + 10
        while time.time() < end:
            s = c.get(f"/sessions/{sid}", headers=h).json()
            if s["deeper_state"] == "failed" and s["replay_state"] == "ready":
                break
            time.sleep(0.02)
        assert s["deeper_state"] == "failed" and s["replay_state"] == "ready" and s["visuals_state"] == "done"


def test_without_pictures_learn_more_still_runs_by_itself(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, visuals_enabled=False, deeper_auto=True))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = process(c, h)
        assert wait_deeper(c, h, sid)["state"] == "done"
        assert [x[0] for x in fake_lk.calls if x[0] in ("visuals", "deeper", "replay")] == ["deeper"]
