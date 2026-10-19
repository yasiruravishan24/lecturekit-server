from fastapi.testclient import TestClient
from lecturekit.chunks import CHUNK_SECONDS, OVERLAP
from lecturekit.errors import LLMError

from server.main import create_app
from tests.conftest import file_part, make_session, make_settings, new_user, wait_idle, wait_job


def open_live(client, h):
    sid = make_session(client, h)
    r = client.post(f"/sessions/{sid}/live", headers=h)
    assert r.status_code == 200, r.text
    return sid


def chunk(client, h, sid, index, name="c.wav", data=b"audio"):
    return client.post(f"/sessions/{sid}/live/chunks", params={"index": index}, headers=h, files={"chunk": file_part(name, data)})


def test_open_live_returns_the_chunk_contract(client):
    h = new_user(client)
    sid = make_session(client, h)
    r = client.post(f"/sessions/{sid}/live", headers=h)
    assert r.json() == {"chunk_seconds": CHUNK_SECONDS, "overlap": OVERLAP}              # the client must use these
    assert client.post(f"/sessions/{sid}/live", headers=h).json() == r.json()           # opening again changes nothing
    sess = client.get(f"/sessions/{sid}", headers=h).json()
    assert sess["state"] == "live" and sess["source"] == "live"


def test_live_cannot_be_opened_on_a_session_that_has_a_recording(client):
    h = new_user(client)
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    r = client.post(f"/sessions/{sid}/live", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_empty"


def test_chunks_in_any_order_and_repeated(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    assert chunk(client, h, sid, 1).json()["queued"] == 1
    chunk(client, h, sid, 0)
    chunk(client, h, sid, 1, data=b"again")                                              # repeat = redo
    wait_idle(app)
    st = client.get(f"/sessions/{sid}/live", headers=h).json()
    assert st["received"] == [0, 1] and st["transcribed"] == [0, 1] and st["failed"] == [] and st["missing"] == []
    assert [c[1] for c in fake_lk.calls if c[0] == "chunk"] == [1, 0, 1]                  # whisper ran in arrival order
    uid = client.get("/auth/me", headers=h).json()["user"]["id"]
    folder = app.state.settings.users_dir / str(uid) / "sessions" / str(sid) / "upload"
    assert sorted(p.name for p in folder.iterdir()) == ["chunk_00000.wav", "chunk_00001.wav"]
    assert (folder / "chunk_00001.wav").read_bytes() == b"again"                         # the repeat replaced the file


def test_status_lists_missing_chunks(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    chunk(client, h, sid, 0)
    chunk(client, h, sid, 2)
    wait_idle(app)
    st = client.get(f"/sessions/{sid}/live", params={"total": 4}, headers=h).json()
    assert st["received"] == [0, 2] and st["missing"] == [1, 3] and st["total"] == 4
    assert client.get(f"/sessions/{sid}/live", headers=h).json()["missing"] == []        # no total given: nothing to compare


def test_finish_with_missing_chunks_gives_a_readable_error(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    chunk(client, h, sid, 0)
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 3}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_input"
    assert r.json()["error"]["message"] == "chunks 1 and 2 are missing, send them and finish again"
    chunk(client, h, sid, 1)
    chunk(client, h, sid, 3)
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 5}, headers=h)
    assert r.json()["error"]["message"] == "chunks 2 and 4 are missing, send them and finish again"
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 4}, headers=h)
    assert r.json()["error"]["message"] == "chunk 2 is missing, send it and finish again"
    assert client.get(f"/sessions/{sid}", headers=h).json()["state"] == "live"           # nothing was queued


def test_finish_after_all_chunks_gives_notes_in_fifo_order(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    fake_lk.gate.clear()                                                                 # chunks are still being transcribed ...
    chunk(client, h, sid, 0)
    chunk(client, h, sid, 1)
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h,
                    files=[("slides", file_part("a.pdf")), ("slides", file_part("b.pptx"))])
    assert r.status_code == 200, r.text                                                  # ... and finish is accepted: the queue is FIFO
    assert client.get(f"/sessions/{sid}", headers=h).json()["state"] == "queued"
    fake_lk.gate.set()
    job = wait_job(client, h, r.json()["job_id"])
    assert job["state"] == "done" and job["kind"] == "finish"
    assert [c[0] for c in fake_lk.calls] == ["chunk", "chunk", "finish"]                  # finish ran after the chunks sent before it
    assert fake_lk.calls[-1] == ("finish", 2, ["slide_01.pdf", "slide_02.pptx"], True)
    sess = client.get(f"/sessions/{sid}", headers=h).json()
    assert sess["state"] == "done" and sess["config"] == "C" and sess["has_slides"] is True and sess["has_notes"] is True
    assert client.get(f"/sessions/{sid}/notes", headers=h).json()["title"] == "Test lecture"
    assert len(client.get(f"/sessions/{sid}/quiz", headers=h).json()["questions"]) == 3
    # nothing more can be sent to a finished live recording
    assert chunk(client, h, sid, 5).status_code == 409
    assert client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h).status_code == 409


def test_a_failed_chunk_counts_as_missing_until_it_is_sent_again(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    fake_lk.chunk_fail[1] = LLMError("x")                                                # chunk 1 fails in Whisper
    chunk(client, h, sid, 0)
    jid = chunk(client, h, sid, 1).json()["job_id"]
    assert wait_job(client, h, jid)["state"] == "failed"
    st = client.get(f"/sessions/{sid}/live", params={"total": 2}, headers=h).json()
    assert st["received"] == [0, 1] and st["failed"] == [1] and st["missing"] == [1]
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["message"] == "chunk 1 is missing, send it and finish again"
    fake_lk.chunk_fail.clear()
    chunk(client, h, sid, 1)                                                             # send it again
    wait_idle(app)
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h)
    assert r.status_code == 200 and wait_job(client, h, r.json()["job_id"])["state"] == "done"


def test_finish_failing_on_a_missing_chunk_can_be_repaired(client, app, fake_lk, settings):
    h = new_user(client)
    sid = open_live(client, h)
    chunk(client, h, sid, 0)
    chunk(client, h, sid, 1)
    wait_idle(app)
    uid = client.get("/auth/me", headers=h).json()["user"]["id"]
    (settings.users_dir / str(uid) / "sessions" / str(sid) / "chunks" / "chunk_00001.json").unlink()   # lost on disk
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h)
    job = wait_job(client, h, r.json()["job_id"])
    assert job["state"] == "failed" and job["error_code"] == "missing_input" and "chunks missing: [1]" in job["error_message"]
    assert client.get(f"/sessions/{sid}", headers=h).json()["state"] == "failed"
    assert chunk(client, h, sid, 1).status_code == 200                                  # the client may send it again
    wait_idle(app)
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h)
    assert wait_job(client, h, r.json()["job_id"])["state"] == "done"


def test_retry_of_a_failed_live_finish_uses_the_stored_total(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    chunk(client, h, sid, 0)
    chunk(client, h, sid, 1)
    wait_idle(app)
    fake_lk.fail_with = LLMError("x")
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 2}, headers=h)
    assert wait_job(client, h, r.json()["job_id"])["error_code"] == "llm"
    fake_lk.fail_with = None
    retried = client.post(f"/sessions/{sid}/retry", headers=h)
    assert retried.status_code == 200 and wait_job(client, h, retried.json()["job_id"])["state"] == "done"
    assert fake_lk.calls[-1][:2] == ("finish", 2)


def test_chunk_validation(tmp_path, fake_lk):
    app = create_app(make_settings(tmp_path, max_chunk_mb=1, max_chunks_per_session=3))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = open_live(c, h)
        assert chunk(c, h, sid, 0, "notes.txt").status_code == 422                        # wrong type
        assert chunk(c, h, sid, -1).status_code == 422                                    # bad index
        r = chunk(c, h, sid, 0, data=b"x" * (1024 * 1024 + 5))
        assert r.status_code == 413 and r.json()["error"]["message"] == "File too large (limit 1 MB)"
        folder = tmp_path / "data"
        assert not [p for p in folder.rglob("*") if p.is_file() and p.name.startswith(("chunk_", ".chunk"))]   # nothing left behind
        assert c.get(f"/sessions/{sid}/live", headers=h).json()["received"] == []
        for i in range(3):
            assert chunk(c, h, sid, i).status_code == 200
        r = chunk(c, h, sid, 3)                                                           # a fourth distinct chunk
        assert r.status_code == 429 and r.json()["error"]["code"] == "chunk_limit"
        assert chunk(c, h, sid, 2).status_code == 200                                     # redoing an existing one is fine


def test_live_and_whole_file_routes_do_not_mix(client):
    h = new_user(client)
    live_sid, plain_sid = open_live(client, h), make_session(client, h, "plain")
    assert chunk(client, h, plain_sid, 0).json()["error"]["code"] == "not_live"
    assert client.get(f"/sessions/{plain_sid}/live", headers=h).status_code == 409
    assert client.post(f"/sessions/{plain_sid}/live/finish", params={"total": 1}, headers=h).status_code == 409
    up = client.post(f"/sessions/{live_sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    assert up.status_code == 409 and up.json()["error"]["code"] == "live_session"
    pr = client.post(f"/sessions/{live_sid}/process", headers=h)
    assert pr.status_code == 409 and pr.json()["error"]["code"] == "live_session"


def test_clients_can_tell_that_a_recording_is_going_on(client, app, fake_lk):
    h = new_user(client)
    sid = open_live(client, h)
    first = client.get(f"/sessions/{sid}", headers=h).json()["live_at"]
    assert first                                                              # a recording that just started: the time it was opened
    assert client.get(f"/sessions/{sid}/live", headers=h).json()["last_chunk_at"] is None
    chunk(client, h, sid, 0)
    status = client.get(f"/sessions/{sid}/live", headers=h).json()
    assert status["last_chunk_at"] and status["last_chunk_at"] >= first
    assert client.get(f"/sessions/{sid}", headers=h).json()["live_at"] == status["last_chunk_at"]
    other = make_session(client, h)
    assert client.get(f"/sessions/{other}", headers=h).json()["live_at"] is None            # only live recordings have it
