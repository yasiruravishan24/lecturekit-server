from tests.conftest import file_part, make_session, new_user, wait_job


def sdir(settings, client, h, sid):
    uid = client.get("/auth/me", headers=h).json()["user"]["id"]
    return settings.users_dir / str(uid) / "sessions" / str(sid)


def test_upload_saves_files_under_server_made_names(client, settings):
    h = new_user(client)
    sid = make_session(client, h)
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[
        ("recording", file_part("../../evil name.MP4", b"video")), ("slides", file_part("a.pdf", b"%PDF")),
        ("slides", file_part("b.pptx", b"ppt")), ("slides", file_part("c.docx", b"doc"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "uploaded" and body["has_slides"] is True
    assert [(f["kind"], f["bytes"]) for f in body["files"]] == [("recording", 5), ("slides", 4), ("slides", 3), ("slides", 3)]
    assert body["files"][0]["original_name"] == "../../evil name.MP4"            # kept as text only
    on_disk = sorted(p.name for p in (sdir(settings, client, h, sid) / "upload").iterdir())
    assert on_disk == ["recording.mp4", "slide_01.pdf", "slide_02.pptx", "slide_03.docx"]
    assert not list(settings.data_dir.rglob("evil*"))                           # the client's name never became a path


def test_more_slides_are_added_in_order_and_a_new_recording_replaces_the_old(client, settings, fake_lk):
    h = new_user(client)
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("one.mp4", b"1")), ("slides", file_part("a.pdf"))])
    folder = sdir(settings, client, h, sid)
    (folder / "transcript.json").write_text("{}")
    (folder / "out").mkdir()
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[("slides", file_part("b.pdf")), ("slides", file_part("c.pdf"))])
    assert [f["stored_name"] if "stored_name" in f else f["original_name"] for f in r.json()["files"]] == ["one.mp4", "a.pdf", "b.pdf", "c.pdf"]
    assert sorted(p.name for p in (folder / "upload").iterdir()) == ["recording.mp4", "slide_01.pdf", "slide_02.pdf", "slide_03.pdf"]
    assert not (folder / "transcript.json").exists() and not (folder / "out").exists()      # nothing stale is reused
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("two.m4a", b"22"))])
    names = sorted(p.name for p in (folder / "upload").iterdir())
    assert "recording.m4a" in names and "recording.mp4" not in names               # replaced, not added
    files = client.get(f"/sessions/{sid}", headers=h).json()["files"]
    assert [f["original_name"] for f in files if f["kind"] == "recording"] == ["two.m4a"]


def test_wrong_types_are_rejected_and_nothing_is_left(client, settings):
    h = new_user(client)
    sid = make_session(client, h)
    cases = [[("recording", file_part("notes.txt"))], [("recording", file_part("noext"))],
             [("recording", file_part("ok.mp4")), ("slides", file_part("bad.exe"))]]
    for files in cases:
        r = client.post(f"/sessions/{sid}/upload", headers=h, files=files)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation", files
    folder = sdir(settings, client, h, sid) / "upload"
    assert not folder.exists() or list(folder.iterdir()) == []                  # the good recording in case 3 was not kept
    assert client.get(f"/sessions/{sid}", headers=h).json()["state"] == "empty"
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.xyz"))])
    assert ".mp4" in r.json()["error"]["message"] and ".pdf" not in r.json()["error"]["message"]


def test_too_large_files_get_413_and_no_partial_file(tmp_path, fake_lk):
    from fastapi.testclient import TestClient
    from server.main import create_app
    from tests.conftest import make_settings
    app = create_app(make_settings(tmp_path, max_recording_mb=1, max_slides_mb=1, max_chunk_mb=1))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = make_session(c, h)
        big = b"x" * (1024 * 1024 + 5)
        r = c.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("big.mp4", big))])
        assert r.status_code == 413 and r.json()["error"] == {"code": "too_large", "message": "File too large (limit 1 MB)"}
        r = c.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("ok.mp4", b"ok")), ("slides", file_part("big.pdf", big))])
        assert r.status_code == 413
        leftovers = [p for p in (tmp_path / "data").rglob("*") if p.is_file() and p.suffix in (".mp4", ".pdf", ".part")]
        assert leftovers == []                                                  # no partial file, and the good one was not kept
        # a request that announces a huge body is refused before it is read
        r = c.post(f"/sessions/{sid}/upload", headers={**h, "content-length": str(500 * 1024 * 1024)}, content=b"x")
        assert r.status_code == 413


def test_empty_upload_is_rejected(client):
    h = new_user(client)
    sid = make_session(client, h)
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", ("", b"", "application/octet-stream"))])
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_input"
    assert client.post(f"/sessions/{sid}/upload", headers=h).status_code == 422


def test_process_needs_a_recording_and_cannot_start_twice(client, app, fake_lk):
    h = new_user(client)
    sid = make_session(client, h)
    r = client.post(f"/sessions/{sid}/process", headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_input"
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4")), ("slides", file_part("s.pdf"))])
    fake_lk.gate.clear()                                                        # keep the first job running
    first = client.post(f"/sessions/{sid}/process", headers=h)
    assert first.status_code == 200 and "job_id" in first.json()
    second = client.post(f"/sessions/{sid}/process", headers=h)
    assert second.status_code == 409 and second.json()["error"]["code"] == "busy"
    assert client.post(f"/sessions/{sid}/upload", headers=h, files=[("slides", file_part("t.pdf"))]).status_code == 409
    assert client.delete(f"/sessions/{sid}", headers=h).status_code == 409
    fake_lk.gate.set()
    job = wait_job(client, h, first.json()["job_id"])
    assert job["state"] == "done" and job["percent"] == 100
    assert fake_lk.calls[0][:3] == ("process", "recording.mp4", ["slide_01.pdf"]) and fake_lk.calls[0][4] is True   # final model
    sess = client.get(f"/sessions/{sid}", headers=h).json()
    assert sess["state"] == "done" and sess["config"] == "C" and sess["has_notes"] is True


def test_retry_only_after_a_failure(client, fake_lk):
    from lecturekit.errors import LLMError
    h = new_user(client)
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    assert client.post(f"/sessions/{sid}/retry", headers=h).status_code == 409           # not failed yet
    fake_lk.fail_with = LLMError("boom")
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "failed" and job["error_code"] == "llm"
    assert client.get(f"/sessions/{sid}", headers=h).json()["state"] == "failed"
    fake_lk.fail_with = None
    retried = client.post(f"/sessions/{sid}/retry", headers=h)
    assert retried.status_code == 200
    assert wait_job(client, h, retried.json()["job_id"])["state"] == "done"
    assert client.post(f"/sessions/{sid}/retry", headers=h).status_code == 409           # done now
