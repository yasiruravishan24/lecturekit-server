"""Offline test setup (SERVER_PLAN.md section 12): temporary database and storage, a real worker thread, and
fakes for the three lecturekit calls that write the same files and results (no Whisper, no Claude, no network)."""
import io
import re
import json
import os
import tempfile
import threading
import time
from pathlib import Path

# lecturekit reads these when it is first imported: keep its logs and cache out of the project during tests
_scratch = tempfile.mkdtemp(prefix="lk-server-tests-")
os.environ["LK_LOG"] = os.path.join(_scratch, "logs")
os.environ["LK_CACHE"] = os.path.join(_scratch, "cache")

import pytest
from fastapi.testclient import TestClient

from lecturekit import api as lk
from lecturekit.errors import MissingInputError

from server.main import create_app
from server.settings import Settings

PASSWORD = "correct horse battery"


def make_settings(tmp_path, **over) -> Settings:
    base = dict(data_dir=tmp_path / "data", jwt_secret="s" * 40, bcrypt_rounds=4,
                preload_whisper=False, housekeeping_seconds=3600, visuals_enabled=False)
    base.update(over)
    return Settings(**base)


def fake_result(out_dir, config: str = "A", n: int = 3) -> dict:
    out_dir = Path(out_dir)
    out = out_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{config}.md").write_text("# Test lecture\n\nnotes\n")
    (out_dir / "transcript.json").write_text("{}")
    return {"status": "done", "session_dir": str(out_dir), "config": config, "model": "fake-model",
            "notes": {"title": "Test lecture",
                      "full_notes": [{"heading": "Loops", "summary": "Loops repeat.", "key_points": ["while", "for"]}],
                      "short_notes": ["Loops repeat", "Use for"]},
            "quiz": [{"question": f"Question {i}?", "options": ["a", "b", "c", "d"], "answer_index": i % 4,
                      "bloom_level": "remember", "explanation": f"Because {i}."} for i in range(n)],
            "stats": {"ratio": 0.2, "total_seconds": 1.0},
            "files": {"json": str(out / f"{config}.json"), "markdown": str(out / f"{config}.md")}}


class FakeLK:
    """Stands in for lecturekit.api.process_session / add_chunk / finish_chunked_session."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.gate = threading.Event()            # clear it to make a job wait, so tests can look at it mid-run
        self.gate.set()
        self.running = 0
        self.max_running = 0
        self.fail_with: Exception | None = None
        self.chunk_fail: dict[int, Exception] = {}
        self.stages = [("transcribe", 12, "speech to text"), ("notes", 62, "writing notes"), ("quiz", 75, "writing quiz")]

    def _enter(self):
        self.running += 1
        self.max_running = max(self.max_running, self.running)

    def process_session(self, recording, out_dir, slides=None, config_name=None, title=None, n_questions=10,
                        final=False, whisper_size=None, llm=None, reuse=True, on_progress=None):
        self._enter()
        try:
            self.calls.append(("process", Path(recording).name, [Path(s).name for s in (slides or [])], title, final))
            for stage, pct, msg in self.stages:
                if on_progress:
                    on_progress(stage, pct, msg)
            self.gate.wait(10)
            if self.fail_with:
                raise self.fail_with
            return fake_result(out_dir, "C" if slides else "A")
        finally:
            self.running -= 1

    def add_chunk(self, session_dir, index, chunk_file, chunk_seconds=300, overlap=5, title=None, whisper_size=None):
        self._enter()
        try:
            self.calls.append(("chunk", index, Path(chunk_file).name))
            self.gate.wait(10)
            if index in self.chunk_fail:
                raise self.chunk_fail[index]
            d = Path(session_dir) / "chunks"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"chunk_{index:05d}.json").write_text(json.dumps({"segments": []}))
            return {"index": index, "offset": 0, "segments": 0, "audio_seconds": 10.0, "seconds": 1.5, "received": [index]}
        finally:
            self.running -= 1

    def finish_chunked_session(self, session_dir, total_chunks, slides=None, config_name=None, chunk_seconds=300,
                               overlap=5, n_questions=10, final=False, llm=None, on_progress=None):
        self._enter()
        try:
            self.calls.append(("finish", total_chunks, [Path(s).name for s in (slides or [])], final))
            for stage, pct, msg in self.stages:
                if on_progress:
                    on_progress(stage, pct, msg)
            self.gate.wait(10)
            have = {int(p.stem.split("_")[1]) for p in (Path(session_dir) / "chunks").glob("chunk_*.json")}
            missing = [k for k in range(total_chunks) if k not in have]
            if missing:
                raise MissingInputError(f"chunks missing: {missing}; upload them and finish again")
            if self.fail_with:
                raise self.fail_with
            return fake_result(session_dir, "C" if slides else "A")
        finally:
            self.running -= 1


class FakeBackground:
    """Stands in for the picture and replay calls of lecturekit.api (no OpenCV, no ffmpeg, no Claude)."""

    def __init__(self, owner):
        self.owner = owner
        self.gate = threading.Event()               # clear it to hold the background lane, so tests can look at it
        self.gate.set()
        self.visuals_fail: Exception | None = None
        self.replay_fail: Exception | None = None
        self.shots = [{"t": 12.0, "kind": "board", "text": "F = m a", "description": "", "file": "shot_001.jpg"},
                      {"t": 95.5, "kind": "slide", "text": "Loops", "description": "a flow chart", "file": "shot_002.jpg"}]

    def extract_visuals(self, video, out_dir, final=True, llm=None, max_frames=None, on_progress=None):
        self.owner.calls.append(("visuals", Path(video).name))
        for stage, pct in (("frames", 5), ("blur", 30), ("read", 45), ("save", 90)):
            if on_progress:
                on_progress(stage, pct)
        self.gate.wait(10)
        if self.visuals_fail:
            raise self.visuals_fail
        shots = Path(out_dir) / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        for s in self.shots:
            (shots / s["file"]).write_bytes(b"\xff\xd8fakejpeg" + s["file"].encode())
        return {"shots": list(self.shots)}

    def refine_notes_with_visuals(self, out_dir, notes, final=True, llm=None, config_name=None):
        self.owner.calls.append(("refine", config_name))
        return {"title": notes["title"], "full_notes": notes["full_notes"] + [{"heading": "From the board", "summary": "F = m a", "key_points": ["force"]}],
                "short_notes": notes["short_notes"]}

    def make_replay(self, src, dst_dir, name="replay", on_progress=None):
        self.owner.calls.append(("replay", Path(src).name))
        if on_progress:
            on_progress(50)
        self.gate.wait(10)
        if self.replay_fail:
            raise self.replay_fail
        audio = Path(src).suffix.lower() in (".m4a", ".mp3", ".wav", ".aac", ".ogg")
        f = Path(dst_dir) / (name + (".m4a" if audio else ".mp4"))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(bytes(range(256)) * 40)
        return {"kind": "audio" if audio else "video", "file": f.name, "bytes": f.stat().st_size}


@pytest.fixture
def fake_lk(monkeypatch):
    fake = FakeLK()
    monkeypatch.setattr(lk, "process_session", fake.process_session)
    monkeypatch.setattr(lk, "add_chunk", fake.add_chunk)
    monkeypatch.setattr(lk, "finish_chunked_session", fake.finish_chunked_session)
    fake.bg = FakeBackground(fake)
    monkeypatch.setattr(lk, "extract_visuals", fake.bg.extract_visuals)
    monkeypatch.setattr(lk, "refine_notes_with_visuals", fake.bg.refine_notes_with_visuals)
    monkeypatch.setattr(lk, "make_replay", fake.bg.make_replay)
    return fake


@pytest.fixture(autouse=True)
def _outbox(monkeypatch):
    """Every app made in a test keeps its emails in app.state.mailer (an Outbox) instead of sending them."""
    monkeypatch.setattr("server.main.make_mailer", lambda settings: Outbox())


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def app(settings, fake_lk):
    return create_app(settings)


@pytest.fixture
def client(app):
    """Test client whose paths are relative to the API prefix: client.get('/courses') -> GET /api/courses."""
    with TestClient(app, base_url="http://testserver/api") as c:
        yield c


@pytest.fixture
def root_client(app):
    with TestClient(app) as c:
        yield c


class Outbox:
    """Stands in for the mailer: keeps what would have been emailed."""

    def __init__(self):
        self.sent: list[tuple[str, str, str, str | None]] = []

    def send(self, to, subject, text, html=None):
        self.sent.append((to, subject, text, html))

    def code_for(self, email) -> str:
        text = [m for m in self.sent if m[0] == email and "verify" in m[1].lower()][-1][2]
        return re.search(r"\b(\d{6})\b", text).group(1)


def register_body(email="a@example.com", password=PASSWORD, **over):
    return {"full_name": "Test User", "email": email, "password": password, "accept_terms": True, **over}


def register(client, email="a@example.com", password=PASSWORD, verify=True):
    r = client.post("/auth/register", json=register_body(email, password))
    assert r.status_code == 201, r.text
    if verify:
        r2 = client.post("/auth/verify-email", json={"email": email, "code": client.app.state.mailer.code_for(email.lower())})
        assert r2.status_code == 200, r2.text
        return r2.json()["user"]
    return r.json()["user"]


def login(client, email="a@example.com", password=PASSWORD) -> dict:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def new_user(client, email="a@example.com") -> dict:
    register(client, email)
    return login(client, email)


def make_session(client, headers, title="Lecture 1") -> int:
    c = client.post("/courses", json={"name": "CS"}, headers=headers).json()
    m = client.post(f"/courses/{c['id']}/modules", json={"name": "Week 1"}, headers=headers).json()
    return client.post(f"/modules/{m['id']}/sessions", json={"title": title}, headers=headers).json()["id"]


def file_part(name="x.mp4", data=b"0123456789"):
    return (name, io.BytesIO(data), "application/octet-stream")


def wait_idle(app, timeout=10):
    """Wait until the worker has finished everything queued so far."""
    done = threading.Event()
    threading.Thread(target=lambda: (app.state.worker.q.join(), done.set()), daemon=True).start()
    assert done.wait(timeout), "worker did not become idle"


def wait_job(client, headers, job_id, timeout=10) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/jobs/{job_id}", headers=headers).json()
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {job}")
