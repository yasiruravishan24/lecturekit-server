import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lecturekit.errors import ExternalToolError, LLMError, MissingInputError, ValidationError

from server.main import create_app
from server.models import Job, LectureSession, utcnow
from tests.conftest import file_part, make_session, make_settings, new_user, wait_idle, wait_job


def uploaded(client, h, title="L"):
    sid = make_session(client, h, title)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4", b"v"))])
    return sid


def test_only_one_job_runs_at_a_time_and_the_second_waits(client, app, fake_lk):
    h = new_user(client)
    s1, s2 = uploaded(client, h, "one"), uploaded(client, h, "two")
    fake_lk.gate.clear()
    j1 = client.post(f"/sessions/{s1}/process", headers=h).json()["job_id"]
    j2 = client.post(f"/sessions/{s2}/process", headers=h).json()["job_id"]
    deadline = time.time() + 5
    while client.get(f"/jobs/{j1}", headers=h).json()["state"] != "processing" and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)                                              # give a (wrongly) parallel worker the chance to start
    assert client.get(f"/jobs/{j1}", headers=h).json()["state"] == "processing"
    assert client.get(f"/jobs/{j2}", headers=h).json()["state"] == "queued"           # waits its turn
    assert client.get(f"/sessions/{s2}", headers=h).json()["state"] == "queued"
    assert client.get("/health", headers=h).json()["queue_length"] == 2
    fake_lk.gate.set()
    wait_job(client, h, j1)
    wait_job(client, h, j2)
    assert fake_lk.max_running == 1                                                    # never two at once
    assert [c[3] for c in fake_lk.calls] == ["one", "two"]                              # FIFO order


def test_progress_is_written_to_the_job_row(client, fake_lk):
    h = new_user(client)
    sid = uploaded(client, h)
    fake_lk.gate.clear()                                         # the fake reports its stages, then waits
    jid = client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"]
    deadline, job = time.time() + 5, {}
    while time.time() < deadline:
        job = client.get(f"/jobs/{jid}", headers=h).json()
        if job["stage"] == "quiz":
            break
        time.sleep(0.02)
    assert (job["state"], job["stage"], job["percent"], job["message"]) == ("processing", "quiz", 75, "writing quiz")
    assert job["started_at"] and job["finished_at"] is None
    assert client.get(f"/sessions/{sid}", headers=h).json()["latest_job"]["stage"] == "quiz"
    fake_lk.gate.set()
    done = wait_job(client, h, jid)
    assert (done["state"], done["stage"], done["percent"]) == ("done", "done", 100) and done["finished_at"]


@pytest.mark.parametrize("error, code, message", [
    (MissingInputError("recording not found: x"), "missing_input", "recording not found: x"),
    (MissingInputError("ANTHROPIC_API_KEY is not set (add it to .env)"), "llm", "The AI service is not configured, try again later"),
    (ValidationError("Configuration C needs slides"), "validation", "Configuration C needs slides"),
    (ExternalToolError("ffmpeg could not read media.mp4: moov atom not found"), "external_tool", "ffmpeg could not read this file"),
    (ExternalToolError("LibreOffice could not convert deck.pptx: boom"), "external_tool", "Slides could not be converted"),
    (ExternalToolError("LibreOffice is needed to read PowerPoint/Word files (brew install --cask libreoffice), or export the slides to PDF yourself"),
     "external_tool", "This server cannot read PowerPoint or Word files; please upload the slides as a PDF"),
    (LLMError("Claude API call 'notes' failed: RateLimitError: sk-ant-secret"), "llm", "The AI service failed, try again later"),
])
def test_lecturekit_errors_become_failed_jobs_with_a_safe_message(client, fake_lk, error, code, message):
    h = new_user(client)
    sid = uploaded(client, h)
    fake_lk.fail_with = error
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "failed" and job["error_code"] == code and job["error_message"] == message
    assert "sk-ant" not in str(job) and "provider" not in str(job).lower()
    sess = client.get(f"/sessions/{sid}", headers=h).json()
    assert sess["state"] == "failed" and sess["latest_job"]["error_code"] == code


def test_an_unexpected_exception_is_reported_without_details_and_the_worker_survives(client, fake_lk, caplog):
    h = new_user(client)
    s1, s2 = uploaded(client, h, "bad"), uploaded(client, h, "good")
    fake_lk.fail_with = RuntimeError("secret internal detail /etc/passwd")
    job = wait_job(client, h, client.post(f"/sessions/{s1}/process", headers=h).json()["job_id"])
    assert (job["state"], job["error_code"], job["error_message"]) == ("failed", "unexpected", "Unexpected error")
    assert "secret internal detail" not in str(job)
    assert any("secret internal detail" in r.getMessage() or r.exc_info for r in caplog.records)      # the details are in the log
    fake_lk.fail_with = None
    assert wait_job(client, h, client.post(f"/sessions/{s2}/process", headers=h).json()["job_id"])["state"] == "done"


def test_restart_marks_unfinished_jobs_failed(tmp_path, fake_lk):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        s_running, s_queued, s_done = uploaded(c, h, "running"), uploaded(c, h, "queued"), uploaded(c, h, "done")
        uid = c.get("/auth/me", headers=h).json()["user"]["id"]
        wait_job(c, h, c.post(f"/sessions/{s_done}/process", headers=h).json()["job_id"])
    # the server stopped while two lectures were not finished: write that state the way a crash would leave it
    with app.state.session_factory() as db:
        for sid, state in ((s_running, "processing"), (s_queued, "queued")):
            db.get(LectureSession, sid).state = state
            db.add(Job(session_id=sid, user_id=uid, kind="process", state=state))
        db.commit()
    app2 = create_app(settings)                                  # a new process on the same data
    with TestClient(app2, base_url="http://testserver/api") as c2:
        for sid in (s_running, s_queued):
            sess = c2.get(f"/sessions/{sid}", headers=h).json()
            assert sess["state"] == "failed"
            assert sess["latest_job"]["error_code"] == "server_restarted"
            assert "restarted" in sess["latest_job"]["error_message"]
        assert c2.get(f"/sessions/{s_done}", headers=h).json()["state"] == "done"      # finished work is untouched
        retried = c2.post(f"/sessions/{s_running}/retry", headers=h)                    # and the user can retry
        assert retried.status_code == 200 and wait_job(c2, h, retried.json()["job_id"])["state"] == "done"


def test_a_job_for_a_deleted_session_is_dropped_quietly(client, app, fake_lk):
    h = new_user(client)
    sid = uploaded(client, h)
    fake_lk.gate.clear()
    first = client.post(f"/sessions/{uploaded(client, h, 'blocker')}/process", headers=h).json()["job_id"]
    with app.state.session_factory() as db:                       # a queued job whose session is gone
        uid = db.scalar(select(LectureSession.user_id).where(LectureSession.id == sid))
        orphan = Job(session_id=None, user_id=uid, kind="process")
        db.add(orphan)
        db.commit()
        orphan_id = orphan.id
    app.state.worker.submit(orphan_id)
    fake_lk.gate.set()
    wait_job(client, h, first)
    wait_idle(app)
    assert client.get(f"/jobs/{orphan_id}", headers=h).json()["error_code"] == "not_found"


def test_housekeeping_removes_old_media_but_keeps_notes_and_transcript(client, app, settings, fake_lk):
    h = new_user(client)
    sid = uploaded(client, h)
    wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    uid = client.get("/auth/me", headers=h).json()["user"]["id"]
    folder = settings.users_dir / str(uid) / "sessions" / str(sid)
    (folder / "media.mp4").write_bytes(b"big")
    (folder / "chunks").mkdir()
    (folder / "chunks" / "chunk_00000.wav").write_bytes(b"c")
    assert app.state.worker.housekeeping() == 0                                      # too recent: nothing happens
    assert (folder / "media.mp4").exists()
    with app.state.session_factory() as db:                                          # pretend it finished 8 days ago
        for job in db.scalars(select(Job)):
            job.finished_at = utcnow() - timedelta(days=8)
        db.commit()
    assert app.state.worker.housekeeping() == 1
    assert not (folder / "media.mp4").exists() and not (folder / "chunks").exists() and not (folder / "upload" / "recording.mp4").exists()
    assert (folder / "transcript.json").exists() and (folder / "out" / "A.md").exists()      # transcript and notes stay
    assert client.get(f"/sessions/{sid}/notes", headers=h).status_code == 200
    assert client.get(f"/sessions/{sid}/quiz", headers=h).status_code == 200
    assert client.post(f"/sessions/{sid}/process", headers=h).status_code == 422              # the recording is gone: upload again
    assert app.state.worker.housekeeping() == 0                                      # and it is idempotent
