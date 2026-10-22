"""Cancel: notes in the making are stopped and the lecture is deleted; an extra only stops."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server import storage
from server.main import create_app
from server.models import Job
from tests.conftest import file_part, make_session, make_settings, new_user, wait_job
from tests.test_background import process


@pytest.fixture
def client(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, visuals_enabled=False, check_daily_limit=True, daily_limit=2))
    with TestClient(app, base_url="http://testserver/api") as c:
        c.app_ = app
        yield c


def uploaded(client, h, title="L"):
    sid = make_session(client, h, title)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4", b"v"))])
    return sid


def wait_for(fn, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def job_state(client, job_id):
    with client.app_.state.session_factory() as db:
        return db.get(Job, job_id).state


def test_a_job_that_has_not_started_is_cancelled_and_the_lecture_is_deleted(client, fake_lk):
    h = new_user(client)
    first, second = uploaded(client, h, "first"), uploaded(client, h, "second")
    fake_lk.gate.clear()
    client.post(f"/sessions/{first}/process", headers=h)                                   # runs (held by the gate)
    wait_for(lambda: client.get(f"/sessions/{first}", headers=h).json()["state"] == "processing")
    job2 = client.post(f"/sessions/{second}/process", headers=h).json()["job_id"]          # waits its turn
    sdir = storage.session_dir(client.app_.state.settings, 1, second)
    r = client.post(f"/sessions/{second}/cancel", headers=h)
    assert r.status_code == 200 and r.json() == {"state": "deleted"}
    assert client.get(f"/sessions/{second}", headers=h).status_code == 404               # lecture and recording are gone
    assert not sdir.exists()
    assert job_state(client, job2) == "cancelled"
    fake_lk.gate.set()


def test_a_running_job_stops_and_the_lecture_is_deleted(client, fake_lk):
    h = new_user(client)
    sid = uploaded(client, h)
    fake_lk.gate.clear()
    job = client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"]
    assert wait_for(lambda: client.get(f"/jobs/{job}", headers=h).json()["state"] == "processing")
    r = client.post(f"/sessions/{sid}/cancel", headers=h)
    assert r.json() == {"state": "cancelling"}
    assert client.get(f"/jobs/{job}", headers=h).json()["stage"] == "cancelling"
    fake_lk.gate.set()
    assert wait_for(lambda: client.get(f"/sessions/{sid}", headers=h).status_code == 404)
    assert wait_for(lambda: job_state(client, job) == "cancelled")
    assert not storage.session_dir(client.app_.state.settings, 1, sid).exists()


def test_a_cancelled_lecture_does_not_count_against_the_daily_limit(client, fake_lk):
    h = new_user(client)
    for i in range(3):                                                  # the limit is 2 a day: three cancelled ones must not use it up
        sid = uploaded(client, h, f"x{i}")
        fake_lk.gate.clear()
        client.post(f"/sessions/{sid}/process", headers=h)
        wait_for(lambda: client.get(f"/sessions/{sid}", headers=h).json()["state"] == "processing")
        client.post(f"/sessions/{sid}/cancel", headers=h)
        fake_lk.gate.set()
        assert wait_for(lambda: client.get(f"/sessions/{sid}", headers=h).status_code == 404)
    sid = uploaded(client, h, "real")
    assert client.post(f"/sessions/{sid}/process", headers=h).status_code != 429                  # still allowed


def test_there_must_be_something_to_cancel_and_it_must_be_yours(client, fake_lk):
    h = new_user(client)
    sid = uploaded(client, h)
    assert client.post(f"/sessions/{sid}/cancel", headers=h).json()["error"]["code"] == "nothing_to_cancel"      # nothing is running
    other = new_user(client, "b@example.com")
    assert client.post(f"/sessions/{sid}/cancel", headers=other).status_code == 404


def test_cancelling_learn_more_stops_only_that_and_keeps_the_notes(client, fake_lk):
    h = new_user(client)
    sid = process(client, h)
    fake_lk.bg.gate.clear()
    job = client.post(f"/sessions/{sid}/deeper", headers=h).json()["job_id"]
    assert wait_for(lambda: client.get(f"/sessions/{sid}/deeper", headers=h).json()["state"] == "processing")
    assert client.post(f"/sessions/{sid}/cancel?kind=deeper", headers=h).json() == {"state": "cancelling"}
    fake_lk.bg.gate.set()
    assert wait_for(lambda: job_state(client, job) == "cancelled")
    assert client.get(f"/sessions/{sid}/deeper", headers=h).json()["state"] == "none"          # it can be made again
    assert client.get(f"/sessions/{sid}", headers=h).status_code == 200                          # the lecture stays
    assert client.get(f"/sessions/{sid}/notes", headers=h).status_code == 200                    # and so do its notes
    assert client.post(f"/sessions/{sid}/deeper", headers=h).status_code in (200, 202)


def test_a_long_step_is_stopped_at_its_next_report_without_waiting_for_it_to_end(client, fake_lk):
    h = new_user(client)
    sid = uploaded(client, h)
    fake_lk.gate.clear()                                                  # the step never ends by itself (like a long speech to text)
    job = client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"]
    assert wait_for(lambda: client.get(f"/jobs/{job}", headers=h).json()["state"] == "processing")
    client.post(f"/sessions/{sid}/cancel", headers=h)
    assert wait_for(lambda: client.get(f"/sessions/{sid}", headers=h).status_code == 404, timeout=4)     # gone within seconds, the gate was never opened
    assert job_state(client, job) == "cancelled"
    fake_lk.gate.set()
