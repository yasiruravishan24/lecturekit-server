"""The second lane: pictures of the board, notes refined with them, the replay with blurred faces, and the links to them."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.main import create_app
from server.models import Job, LectureSession, MediaFile, Shot, utcnow
from tests.conftest import file_part, make_session, make_settings, new_user, register, login, wait_job


@pytest.fixture
def vclient(tmp_path, fake_lk):
    """A client of a server with the background work switched on."""
    app = create_app(make_settings(tmp_path, visuals_enabled=True))
    with TestClient(app, base_url="http://testserver/api") as c:
        c.app_ = app
        yield c


def process(client, h, name="lecture.mp4", title="L", slides=False):
    sid = make_session(client, h, title)
    files = [("recording", file_part(name, b"video-bytes" * 50))] + ([("slides", file_part("s.pdf", b"%PDF"))] if slides else [])
    assert client.post(f"/sessions/{sid}/upload", headers=h, files=files).status_code == 200
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "done"
    return sid


def wait_bg(client, h, sid, timeout=10):
    """Wait until the pictures and the replay of a lecture are finished (done, ready, failed or skipped)."""
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/sessions/{sid}/replay", headers=h).json()
        if r["visuals_state"] in ("done", "failed", "none") and r["state"] in ("ready", "failed", "none"):
            return r
        time.sleep(0.02)
    raise AssertionError(f"the background work did not finish: {r}")


def test_after_the_notes_the_pictures_then_the_replay_are_made(vclient, fake_lk):
    h = new_user(vclient)
    sid = process(vclient, h)
    info = wait_bg(vclient, h, sid)
    assert info["state"] == "ready" and info["kind"] == "video" and info["visuals_state"] == "done" and info["url"]
    assert [c[0] for c in fake_lk.calls if c[0] in ("visuals", "refine", "replay")] == ["visuals", "refine", "replay"]   # pictures first
    s = vclient.get(f"/sessions/{sid}", headers=h).json()
    assert s["state"] == "done" and s["replay_state"] == "ready" and s["visuals_state"] == "done" and s["shot_count"] == 2
    notes = vclient.get(f"/sessions/{sid}/notes", headers=h).json()
    assert [t["heading"] for t in notes["full_notes"]][-1] == "From the board"           # the board content is in the notes now
    assert len(vclient.get(f"/sessions/{sid}/quiz", headers=h).json()["questions"]) == 3   # the quiz is untouched
    shots = vclient.get(f"/sessions/{sid}/shots", headers=h).json()
    assert [(x["t"], x["kind"], x["text"]) for x in shots] == [(12.0, "board", "F = m a"), (95.5, "slide", "Loops")]
    image = vclient.get(shots[0]["url"].removeprefix("/api"))
    assert image.status_code == 200 and image.content.startswith(b"\xff\xd8") and image.headers["content-type"] == "image/jpeg"
    jobs = [j["kind"] for j in (vclient.get(f"/jobs/{j}", headers=h).json() for j in (1, 2, 3))]
    assert jobs == ["process", "visuals", "replay"]


def test_the_unblurred_original_is_deleted_once_the_replay_exists(vclient, fake_lk):
    h = new_user(vclient)
    sid = process(vclient, h, "lecture.mp4")
    wait_bg(vclient, h, sid)
    sdir = vclient.app_.state.settings.users_dir / "1" / "sessions" / str(sid)
    assert not list((sdir / "upload").glob("recording*")) and (sdir / "replay" / "replay.mp4").exists()
    files = vclient.get(f"/sessions/{sid}", headers=h).json()["files"]
    assert files[0]["removed"] is True and files[0]["bytes"] == 0 and files[0]["original_name"] == "lecture.mp4"
    usage = vclient.get("/auth/usage", headers=h).json()
    assert usage["used_bytes"] == 10240 and usage["limit_bytes"] == 10 * 1024 ** 3        # only the replay counts now


def test_notes_come_first_and_the_background_lane_never_holds_them_up(vclient, fake_lk):
    h = new_user(vclient)
    fake_lk.bg.gate.clear()                                               # the background lane is "stuck" on the first lecture
    first = process(vclient, h, "one.mp4", "one")
    time.sleep(0.3)
    assert vclient.get(f"/sessions/{first}", headers=h).json()["visuals_state"] == "processing"
    start = time.time()
    second = process(vclient, h, "two.mp4", "two")                       # a second lecture still gets its notes at once
    assert time.time() - start < 3
    assert vclient.get(f"/sessions/{second}", headers=h).json()["state"] == "done"
    assert vclient.get(f"/sessions/{second}", headers=h).json()["visuals_state"] == "queued"           # its extras wait their turn
    fake_lk.bg.gate.set()
    assert wait_bg(vclient, h, first)["state"] == "ready" and wait_bg(vclient, h, second)["state"] == "ready"


def test_a_failed_picture_job_leaves_the_notes_and_the_replay_still_gets_made(vclient, fake_lk):
    from lecturekit.errors import LLMError
    fake_lk.bg.visuals_fail = LLMError("boom")
    h = new_user(vclient)
    sid = process(vclient, h)
    info = wait_bg(vclient, h, sid)
    assert info["visuals_state"] == "failed" and info["state"] == "ready"
    s = vclient.get(f"/sessions/{sid}", headers=h).json()
    assert s["state"] == "done" and s["has_notes"] and s["shot_count"] == 0
    assert vclient.get(f"/sessions/{sid}/shots", headers=h).json() == []


def test_a_failed_replay_keeps_the_original(vclient, fake_lk):
    from lecturekit.errors import ExternalToolError
    fake_lk.bg.replay_fail = ExternalToolError("ffmpeg could not read this")
    h = new_user(vclient)
    sid = process(vclient, h)
    info = wait_bg(vclient, h, sid)
    assert info["state"] == "failed" and info["url"] is None
    sdir = vclient.app_.state.settings.users_dir / "1" / "sessions" / str(sid)
    assert list((sdir / "upload").glob("recording*"))                       # nothing is lost
    assert vclient.get(f"/sessions/{sid}", headers=h).json()["files"][0]["removed"] is False


def test_audio_recordings_get_an_audio_replay_and_no_pictures(vclient, fake_lk):
    h = new_user(vclient)
    sid = process(vclient, h, "talk.m4a")
    info = wait_bg(vclient, h, sid)
    assert info["kind"] == "audio" and info["state"] == "ready" and info["visuals_state"] == "done"
    assert vclient.get(f"/sessions/{sid}/shots", headers=h).json() == []
    assert ("visuals", "talk.m4a") not in fake_lk.calls                     # nothing to look at: the AI is not called
    sdir = vclient.app_.state.settings.users_dir / "1" / "sessions" / str(sid)
    assert (sdir / "replay" / "replay.m4a").exists()
    r = vclient.get(info["url"].removeprefix("/api"))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4"


def test_the_replay_can_be_played_and_sought(vclient):
    h = new_user(vclient)
    sid = process(vclient, h)
    url = wait_bg(vclient, h, sid)["url"].removeprefix("/api")
    whole = vclient.get(url)
    assert whole.status_code == 200 and whole.headers["content-type"] == "video/mp4" and whole.headers.get("accept-ranges") == "bytes"
    part = vclient.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == whole.content[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(whole.content)}"
    assert "attachment" in vclient.get(url + "?download=true").headers["content-disposition"]


def test_links_are_private_and_expire(vclient, fake_lk):
    import jwt
    from datetime import datetime, timedelta, timezone
    h = new_user(vclient)
    sid = process(vclient, h)
    url = wait_bg(vclient, h, sid)["url"].removeprefix("/api")
    token = url.rsplit("/", 1)[1]
    assert vclient.get(f"/media/{token}x").status_code == 404                      # tampered
    assert vclient.get("/media/garbage").status_code == 404
    settings = vclient.app_.state.settings
    expired = jwt.encode({"sub": "1", "typ": "media", "sid": sid, "what": "replay", "id": 0,
                          "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, settings.jwt_secret, algorithm="HS256")
    assert vclient.get(f"/media/{expired}").status_code == 404
    login_token = vclient.post("/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}).json()["token"]
    assert vclient.get(f"/media/{login_token}").status_code == 404                 # a login token is not a media link
    assert vclient.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401   # and a media link is not a login
    other = new_user(vclient, "b@example.com")
    assert vclient.get(f"/sessions/{sid}/replay", headers=other).status_code == 404
    assert vclient.get(f"/sessions/{sid}/shots", headers=other).status_code == 404
    from datetime import date
    log = (settings.log_dir / f"server-{date.today().isoformat()}.log").read_text()
    assert token not in log and "/media/..." in log                                # the secret part of a link is never logged


def test_a_new_recording_replaces_the_replay_and_the_pictures(vclient, fake_lk):
    h = new_user(vclient)
    sid = process(vclient, h)
    wait_bg(vclient, h, sid)
    r = vclient.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("new.mp4", b"newvideo"))])
    assert r.status_code == 200
    s = vclient.get(f"/sessions/{sid}", headers=h).json()
    assert s["replay_state"] == "none" and s["visuals_state"] == "none" and s["shot_count"] == 0
    assert vclient.get(f"/sessions/{sid}/shots", headers=h).json() == []
    sdir = vclient.app_.state.settings.users_dir / "1" / "sessions" / str(sid)
    assert not (sdir / "replay").exists() and not (sdir / "shots").exists()


def test_a_lecture_cannot_be_changed_while_its_background_work_runs(vclient, fake_lk):
    h = new_user(vclient)
    fake_lk.bg.gate.clear()
    sid = process(vclient, h)
    time.sleep(0.2)
    r = vclient.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("x.mp4", b"x"))])
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
    assert vclient.post(f"/sessions/{sid}/process", headers=h).status_code == 409
    fake_lk.bg.gate.set()
    wait_bg(vclient, h, sid)
    assert vclient.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("x.mp4", b"x"))]).status_code == 200


def test_try_again_after_the_original_is_gone_uses_the_replay(vclient, fake_lk):
    h = new_user(vclient)
    sid = process(vclient, h)
    wait_bg(vclient, h, sid)
    job = wait_job(vclient, h, vclient.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "done"
    assert [c for c in fake_lk.calls if c[0] == "process"][-1][1] == "replay.mp4"          # the pipeline read the replay
    assert vclient.get(f"/sessions/{sid}", headers=h).json()["replay_state"] == "ready"


def test_storage_limit(vclient):
    h = new_user(vclient)
    sid = make_session(vclient, h)
    vclient.app_.state.settings.__dict__["max_storage_gb"] = 0
    r = vclient.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("x.mp4", b"x"))])
    assert r.status_code == 413 and r.json()["error"]["code"] == "storage_full" and "GB" in r.json()["error"]["message"]


def test_unfinished_background_jobs_are_started_again_after_a_restart(vclient, fake_lk):
    h = new_user(vclient)
    sid = process(vclient, h)
    wait_bg(vclient, h, sid)
    app = vclient.app_
    other = make_session(vclient, h, "other")
    vclient.post(f"/sessions/{other}/upload", headers=h, files=[("recording", file_part("o.mp4", b"o"))])
    with app.state.session_factory() as db:
        s = db.get(LectureSession, other)
        s.state, s.visuals_state = "done", "processing"
        db.add(Job(session_id=other, user_id=s.user_id, kind="visuals", state="processing"))
        db.commit()
    worker = app.state.worker
    assert worker.recover() == 0                                                  # nothing is failed: extra work is simply queued again
    assert len(worker._resume) == 1
    worker.stop()
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        with app.state.session_factory() as db:
            if db.scalar(select(Job.state).where(Job.session_id == other, Job.kind == "visuals")) == "done":
                break
        time.sleep(0.05)
    assert fake_lk.calls.count(("visuals", "recording.mp4")) == 2               # once for the first lecture, once for the interrupted one


def test_housekeeping_keeps_replays_and_still_cleans_old_lectures(vclient, fake_lk):
    from datetime import timedelta
    h = new_user(vclient)
    with_replay = process(vclient, h, "a.mp4", "new")
    wait_bg(vclient, h, with_replay)
    old = process(vclient, h, "b.mp4", "legacy")
    wait_bg(vclient, h, old)
    app = vclient.app_
    with app.state.session_factory() as db:                                      # make the second one look like a lecture from before replays existed
        s = db.get(LectureSession, old)
        s.replay_state = "none"
        s.files.append(MediaFile(kind="recording", original_name="b.mp4", stored_name="recording.mp4", bytes=3))
        for j in db.scalars(select(Job).where(Job.session_id.in_((with_replay, old)))).all():
            j.finished_at = utcnow() - timedelta(days=30)
        db.commit()
    app.state.worker.housekeeping()
    with app.state.session_factory() as db:
        assert db.get(LectureSession, with_replay).replay_state == "ready"
    sdir = app.state.settings.users_dir / "1" / "sessions" / str(with_replay)
    assert (sdir / "replay" / "replay.mp4").exists()                              # the replay is kept until the owner deletes the lecture


def test_deleting_a_lecture_deletes_its_replay_and_pictures(vclient):
    h = new_user(vclient)
    sid = process(vclient, h)
    wait_bg(vclient, h, sid)
    sdir = vclient.app_.state.settings.users_dir / "1" / "sessions" / str(sid)
    assert (sdir / "replay").exists() and (sdir / "shots").exists()
    assert vclient.delete(f"/sessions/{sid}", headers=h).status_code == 204
    assert not sdir.exists()
    assert vclient.get("/auth/usage", headers=h).json()["used_bytes"] == 0


def test_the_replay_can_be_made_on_request_for_an_older_lecture(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path))                                   # background work off: like a lecture made before it existed
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = process(c, h)
        assert c.get(f"/sessions/{sid}/replay", headers=h).json()["state"] == "none"
        r = c.post(f"/sessions/{sid}/replay", headers=h)
        assert r.status_code == 200 and r.json()["job_id"]
        info = wait_bg(c, h, sid)
        assert info["state"] == "ready" and info["visuals_state"] == "done"
        again = c.post(f"/sessions/{sid}/replay", headers=h)
        assert again.status_code == 409 and again.json()["error"]["code"] == "replay_exists"
        other = make_session(c, h, "empty one")
        assert c.post(f"/sessions/{other}/replay", headers=h).status_code == 422          # no notes yet
        assert c.post(f"/sessions/{sid}/replay", headers=new_user(c, "b@example.com")).status_code == 404


def test_an_account_can_be_deleted_while_its_background_work_is_still_running(vclient, fake_lk):
    h = new_user(vclient)
    fake_lk.bg.gate.clear()
    sid = process(vclient, h)
    time.sleep(0.2)
    assert vclient.get(f"/sessions/{sid}", headers=h).json()["visuals_state"] == "processing"
    assert vclient.delete("/auth/me", headers=h).status_code == 204                 # no waiting for the replay
    fake_lk.bg.gate.set()
    time.sleep(0.3)                                                                 # the stuck job ends quietly
    sdir = vclient.app_.state.settings.users_dir / "1"
    assert not (sdir / "sessions" / str(sid) / "replay").exists()
