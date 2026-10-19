from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.main import create_app
from server.models import Job, utcnow
from tests.conftest import file_part, make_session, make_settings, new_user, wait_idle, wait_job


def run(client, h, title="L"):
    sid = make_session(client, h, title)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    return client.post(f"/sessions/{sid}/process", headers=h)


def test_daily_limit_gives_429(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, daily_limit=2))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        for n in range(2):
            r = run(c, h, f"L{n}")
            assert r.status_code == 200
            wait_job(c, h, r.json()["job_id"])
        r = run(c, h, "third")
        assert r.status_code == 429 and r.json()["error"] == {"code": "daily_limit",
                                                              "message": "Daily limit reached (2 lectures); try again tomorrow"}
        other = new_user(c, "b@example.com")                                         # the limit is per user
        assert run(c, other, "mine").status_code == 200


def test_the_limit_counts_only_the_last_24_hours(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, daily_limit=2))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        for n in range(2):
            wait_job(c, h, run(c, h, f"L{n}").json()["job_id"])
        assert run(c, h, "blocked").status_code == 429
        with app.state.session_factory() as db:                                      # one of them was 25 hours ago
            oldest = db.scalars(select(Job).order_by(Job.id)).first()
            oldest.created_at = utcnow() - timedelta(hours=25)
            db.commit()
        r = run(c, h, "allowed again")
        assert r.status_code == 200
        wait_job(c, h, r.json()["job_id"])
        assert run(c, h, "blocked again").status_code == 429


def test_chunk_jobs_do_not_count_but_finish_does(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, daily_limit=1))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = make_session(c, h)
        c.post(f"/sessions/{sid}/live", headers=h)
        for i in range(5):                                                           # many chunks: no limit applies
            assert c.post(f"/sessions/{sid}/live/chunks", params={"index": i}, headers=h,
                          files={"chunk": file_part("c.wav")}).status_code == 200
        wait_idle(app)
        r = c.post(f"/sessions/{sid}/live/finish", params={"total": 5}, headers=h)
        assert r.status_code == 200 and wait_job(c, h, r.json()["job_id"])["state"] == "done"
        assert run(c, h, "next").status_code == 429                                  # the finish used the one lecture of the day


def test_a_failed_job_still_counts(tmp_path, fake_lk):
    from lecturekit.errors import LLMError
    app = create_app(make_settings(tmp_path, daily_limit=1))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        fake_lk.fail_with = LLMError("x")
        assert wait_job(c, h, run(c, h).json()["job_id"])["state"] == "failed"
        assert run(c, h, "again").status_code == 429                                 # every attempt costs: failures count too


def test_the_limit_is_not_checked_on_a_local_computer(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, daily_limit=1, app_env="local"))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        for n in range(3):
            r = run(c, h, f"L{n}")
            assert r.status_code == 200
            wait_job(c, h, r.json()["job_id"])


def test_a_job_lost_to_a_restart_does_not_use_up_the_limit(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, daily_limit=1))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        r = run(c, h, "first")
        wait_job(c, h, r.json()["job_id"])
        assert run(c, h, "second").status_code == 429                         # one job used the day
        with app.state.session_factory() as db:                                    # ... unless it was lost to a restart
            job = db.get(Job, r.json()["job_id"])
            job.state, job.error_code = "failed", "server_restarted"
            db.commit()
        assert run(c, h, "third").status_code == 200
