"""A lecture saved in several recording files: uploaded together, joined in order, processed as one."""
import shutil
import subprocess
from pathlib import Path

import pytest

from server.media import join_recordings
from tests.conftest import file_part, make_session, make_settings, new_user, wait_job

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed to make test sounds")


def tone(path: Path, seconds: float, hz: int = 440, rate: int = 44100, channels: int = 1) -> bytes:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}:sample_rate={rate}",
                    "-ac", str(channels), str(path)], check=True)
    return path.read_bytes()


def length(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout)


def test_join_keeps_the_order_and_mixes_formats(tmp_path):
    a, b, c = tmp_path / "a.wav", tmp_path / "b.mp3", tmp_path / "c.m4a"
    tone(a, 1.0, rate=44100, channels=1)
    tone(b, 2.0, hz=880, rate=48000, channels=2)
    tone(c, 1.5, hz=220, rate=22050, channels=1)
    out = join_recordings([a, b, c], tmp_path / "joined.m4a")
    assert abs(length(out) - 4.5) < 0.4


def test_join_reports_a_file_that_cannot_be_read(tmp_path):
    from lecturekit.errors import ExternalToolError
    good = tmp_path / "a.wav"
    tone(good, 1.0)
    bad = tmp_path / "b.wav"
    bad.write_bytes(b"this is not audio")
    with pytest.raises(ExternalToolError, match="could not join"):
        join_recordings([good, bad], tmp_path / "joined.m4a")
    assert not (tmp_path / "joined.m4a").exists()


def test_several_recordings_are_stored_in_order_and_processed_as_one(client, settings, fake_lk, tmp_path):
    h = new_user(client)
    sid = make_session(client, h)
    parts = [tone(tmp_path / f"p{i}.wav", 1.0 + i, hz=300 + 100 * i) for i in range(3)]
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part(f"part{i}.wav", d)) for i, d in enumerate(parts)]
                    + [("slides", file_part("s.pdf", b"%PDF"))])
    assert r.status_code == 200 and r.json()["state"] == "uploaded"
    assert [(f["kind"], f["original_name"]) for f in r.json()["files"]] == [
        ("recording", "part0.wav"), ("recording", "part1.wav"), ("recording", "part2.wav"), ("slides", "s.pdf")]
    folder = settings.users_dir / "1" / "sessions" / str(sid) / "upload"
    assert sorted(p.name for p in folder.iterdir()) == ["recording.wav", "recording_02.wav", "recording_03.wav", "slide_01.pdf"]
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "done"
    kind, recording, *_ = fake_lk.calls[-1]
    assert kind == "process" and recording == "joined.m4a"                    # the pipeline got one joined file
    assert abs(length(folder / "joined.m4a") - 6.0) < 0.5                      # 1 + 2 + 3 seconds


def test_one_recording_is_not_joined(client, settings, fake_lk, tmp_path):
    h = new_user(client)
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("only.wav", tone(tmp_path / "x.wav", 1.0)))])
    wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert fake_lk.calls[-1][1] == "recording.wav"


def test_a_new_upload_replaces_all_the_old_parts_and_the_joined_file(client, settings, fake_lk, tmp_path):
    h = new_user(client)
    sid = make_session(client, h)
    two = [("recording", file_part(f"p{i}.wav", tone(tmp_path / f"p{i}.wav", 1.0))) for i in range(2)]
    client.post(f"/sessions/{sid}/upload", headers=h, files=two)
    wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    folder = settings.users_dir / "1" / "sessions" / str(sid) / "upload"
    assert (folder / "joined.m4a").exists()
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("new.wav", tone(tmp_path / "n.wav", 1.0)))])
    assert [f["original_name"] for f in r.json()["files"]] == ["new.wav"]
    assert sorted(p.name for p in folder.iterdir()) == ["recording.wav"]


def test_a_broken_part_fails_the_job_with_a_readable_message(client, fake_lk, tmp_path):
    h = new_user(client)
    sid = make_session(client, h)
    good = tone(tmp_path / "a.wav", 1.0)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.wav", good)), ("recording", file_part("b.wav", b"not audio"))])
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "failed" and job["error_code"] == "external_tool" and job["error_message"] == "ffmpeg could not read this file"
    assert client.get(f"/sessions/{sid}", headers=h).json()["state"] == "failed"


def test_limits_on_the_number_and_total_size_of_parts(tmp_path, fake_lk):
    from fastapi.testclient import TestClient
    from server.main import create_app
    app = create_app(make_settings(tmp_path, max_recording_mb=1, max_recording_parts=3))
    with TestClient(app, base_url="http://testserver/api") as c:
        h = new_user(c)
        sid = make_session(c, h)
        r = c.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part(f"p{i}.wav", b"x")) for i in range(4)])
        assert r.status_code == 422 and "at most 3" in r.json()["error"]["message"]
        half = b"x" * (700 * 1024)
        r = c.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.wav", half)), ("recording", file_part("b.wav", half))])
        assert r.status_code == 413 and "together" in r.json()["error"]["message"]
        folder = tmp_path / "data" / "users" / "1" / "sessions" / str(sid) / "upload"
        assert not folder.exists() or list(folder.iterdir()) == []                      # nothing partial is left behind
        bad_type = [("recording", file_part("a.wav", b"x")), ("recording", file_part("b.txt", b"x"))]
        assert c.post(f"/sessions/{sid}/upload", headers=h, files=bad_type).status_code == 422
        assert not folder.exists() or list(folder.iterdir()) == []


def test_a_live_lecture_still_refuses_uploaded_recordings(client, tmp_path):
    h = new_user(client)
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/live", headers=h)
    r = client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.wav", tone(tmp_path / "a.wav", 1.0)))])
    assert r.status_code == 409 and r.json()["error"]["code"] == "live_session"
