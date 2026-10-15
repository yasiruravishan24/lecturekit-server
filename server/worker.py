"""One worker thread and one FIFO queue: only one heavy job at a time (SERVER_PLAN.md section 8, rule 5).

Every unit of work is a `job` row first, then a queue entry. lecturekit is called outside any open database
transaction; the database is only touched in short sessions before and after the call."""
import json
import logging
import queue
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from lecturekit import api as lk
from lecturekit.errors import LectureKitError, MissingInputError

from . import storage
from .errors import job_error
from .models import Attempt, Job, LectureSession, LiveChunk, Note, Question, utcnow
from .settings import Settings

logger = logging.getLogger("lecturekit.server")
PROGRESS_EVERY = 1.0            # seconds: at most one progress write per second per job


class Worker:
    def __init__(self, settings: Settings, session_factory):
        self.settings, self.sf = settings, session_factory
        self.q: "queue.Queue[int | None]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self._running = False

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="lecturekit-worker", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        if self.thread is not None:
            self.q.put(None)
            self.thread.join(timeout=5)
            self.thread = None

    def submit(self, job_id: int) -> None:
        self.q.put(job_id)

    def queue_length(self) -> int:
        return self.q.qsize() + (1 if self._running else 0)

    def _loop(self) -> None:
        while True:
            try:
                job_id = self.q.get(timeout=self.settings.housekeeping_seconds)
            except queue.Empty:
                self._safely(self.housekeeping)
                continue
            try:
                if job_id is None:
                    return
                self._running = True
                self._safely(self.run_job, job_id)
            finally:
                self._running = False
                self.q.task_done()

    @staticmethod
    def _safely(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:                      # the worker must survive anything: the details go to the log
            logger.exception("worker task failed: %s", getattr(fn, "__name__", fn))

    # ---- one job -----------------------------------------------------------------------------
    def run_job(self, job_id: int) -> None:
        info = self._claim(job_id)
        if info is None:
            return
        t0 = time.time()
        try:
            result = self._execute(info)
        except LectureKitError as e:           # an expected problem: stored with its code and a readable message
            code, message = job_error(e.code, str(e))
            logger.warning("job %s (%s) failed: %s: %s", job_id, info["kind"], e.code, e)
            self._fail(info, code, message)
        except Exception:                      # a bug: details only in the log
            logger.exception("job %s (%s) crashed", job_id, info["kind"])
            self._fail(info, "unexpected", "Unexpected error")
        else:
            self._succeed(info, result)
            logger.info("job %s (%s) done in %.1f s", job_id, info["kind"], time.time() - t0)

    def _claim(self, job_id: int) -> dict | None:
        """Mark the job processing and collect everything lecturekit needs as plain values."""
        with self.sf() as db:
            job = db.get(Job, job_id)
            if job is None or job.state != "queued":
                return None
            job.state, job.started_at, job.stage = "processing", utcnow(), "starting"
            session = db.get(LectureSession, job.session_id) if job.session_id else None
            if session is None:
                job.state, job.finished_at = "failed", utcnow()
                job.error_code, job.error_message = "not_found", "The lecture no longer exists"
                db.commit()
                return None
            if job.kind in ("process", "finish"):
                session.state = "processing"
            sdir = storage.session_dir(self.settings, session.user_id, session.id)
            updir = storage.upload_dir(sdir)
            recording = next((updir / f.stored_name for f in session.files if f.kind == "recording"), None)
            info = {"job_id": job.id, "kind": job.kind, "arg": job.arg, "session_id": session.id,
                    "user_id": session.user_id, "title": session.title, "sdir": sdir, "recording": recording,
                    "slides": [updir / f.stored_name for f in session.files if f.kind == "slides"],
                    "chunk_seconds": session.live.chunk_seconds if session.live else None,
                    "overlap": session.live.overlap if session.live else None}
            if job.kind == "chunk":
                info["chunk_file"] = next(iter(sorted(updir.glob(f"chunk_{job.arg:05d}.*"))), None)
            db.commit()
        return info

    def _execute(self, info: dict) -> dict:
        kind, cb = info["kind"], self._progress(info["job_id"])
        slides = info["slides"] or None
        if kind == "process":
            return lk.process_session(info["recording"], info["sdir"], slides=slides, title=info["title"],
                                      final=True, on_progress=cb)
        if kind == "chunk":
            if info["chunk_file"] is None:
                raise MissingInputError(f"chunk {info['arg']} was not found on the server")
            return lk.add_chunk(info["sdir"], info["arg"], info["chunk_file"], info["chunk_seconds"],
                                info["overlap"], title=info["title"])
        return lk.finish_chunked_session(info["sdir"], info["arg"], slides=slides, chunk_seconds=info["chunk_seconds"],
                                         overlap=info["overlap"], final=True, on_progress=cb)

    def _progress(self, job_id: int):
        """lecturekit's on_progress(stage, percent, message) -> job row, throttled to one write per second."""
        last = {"t": 0.0, "stage": None}

        def cb(stage: str, percent: int, message: str = "") -> None:
            now = time.time()
            if stage == last["stage"] and now - last["t"] < PROGRESS_EVERY:
                return
            last["t"], last["stage"] = now, stage
            with self.sf() as db:
                job = db.get(Job, job_id)
                if job is not None:
                    job.stage, job.percent, job.message = stage, int(percent), message or ""
                    db.commit()
        return cb

    def _succeed(self, info: dict, result: dict) -> None:
        with self.sf() as db:
            job = db.get(Job, info["job_id"])
            session = db.get(LectureSession, info["session_id"])
            if job is None:
                return
            if session is not None and info["kind"] == "chunk":
                row = db.scalar(select(LiveChunk).where(LiveChunk.live_id == session.live.id, LiveChunk.idx == info["arg"]))
                if row is not None:
                    row.state, row.seconds, row.error_message = "transcribed", result.get("seconds"), None
                job.stage, job.percent, job.message = "done", 100, ""
            elif session is not None:
                self._store_result(db, session, result)
                job.stage, job.percent, job.message = "done", 100, "finished"
            job.state, job.finished_at = "done", utcnow()
            db.commit()

    @staticmethod
    def _store_result(db, session: LectureSession, result: dict) -> None:
        """Notes and quiz go into the database (replacing an earlier run); an old quiz's attempts go with it."""
        for model in (Note, Question, Attempt):
            for row in db.scalars(select(model).where(model.session_id == session.id)).all():
                db.delete(row)
        db.flush()
        notes = result["notes"]
        db.add(Note(session_id=session.id, title=notes.get("title", ""),
                    full_notes_json=json.dumps(notes.get("full_notes", [])),
                    short_notes_json=json.dumps(notes.get("short_notes", [])),
                    model=result.get("model", ""), stats_json=json.dumps(result.get("stats", {}))))
        for n, q in enumerate(result["quiz"], 1):
            db.add(Question(session_id=session.id, position=n, text=q["question"], options_json=json.dumps(q["options"]),
                            answer_index=int(q["answer_index"]), bloom_level=q.get("bloom_level", ""),
                            explanation=q.get("explanation", "")))
        session.state, session.config = "done", result["config"]

    def _fail(self, info: dict, code: str, message: str) -> None:
        with self.sf() as db:
            job = db.get(Job, info["job_id"])
            session = db.get(LectureSession, info["session_id"])
            if job is not None:
                job.state, job.finished_at, job.error_code, job.error_message = "failed", utcnow(), code, message
            if session is not None:
                if info["kind"] == "chunk":
                    row = db.scalar(select(LiveChunk).where(LiveChunk.live_id == session.live.id, LiveChunk.idx == info["arg"]))
                    if row is not None:
                        row.state, row.error_message = "failed", message
                else:
                    session.state = "failed"
            db.commit()

    # ---- startup recovery and housekeeping -----------------------------------------------------
    def recover(self) -> int:
        """Jobs left `queued` or `processing` by a previous run are failed: the user retries (SERVER_PLAN.md NF3)."""
        with self.sf() as db:
            stale = db.scalars(select(Job).where(Job.state.in_(("queued", "processing")))).all()
            for job in stale:
                job.state, job.finished_at = "failed", utcnow()
                job.error_code, job.error_message = "server_restarted", "The server restarted; please try again"
                session = db.get(LectureSession, job.session_id) if job.session_id else None
                if session is None:
                    continue
                if job.kind == "chunk":
                    row = db.scalar(select(LiveChunk).where(LiveChunk.live_id == session.live.id, LiveChunk.idx == job.arg))
                    if row is not None and row.state == "received":
                        row.state, row.error_message = "failed", "The server restarted; send this chunk again"
                elif session.state in ("queued", "processing"):
                    session.state = "failed"
            db.commit()
            return len(stale)

    def housekeeping(self) -> int:
        """Remove the large files of lectures finished more than KEEP_MEDIA_DAYS ago.

        Notes, quiz and transcript stay. Returns how many sessions were cleaned."""
        cutoff = utcnow() - timedelta(days=self.settings.keep_media_days)
        cleaned = 0
        with self.sf() as db:
            for session in db.scalars(select(LectureSession).where(LectureSession.state == "done")).all():
                last = db.scalar(select(Job.finished_at).where(
                    Job.session_id == session.id, Job.kind.in_(("process", "finish")), Job.state == "done")
                    .order_by(Job.id.desc()).limit(1))
                if last is None or last > cutoff:
                    continue
                sdir = storage.session_dir(self.settings, session.user_id, session.id)
                before = any((sdir / "upload").glob("*")) or any(sdir.glob("media.*")) or (sdir / "chunks").exists()
                for media in sdir.glob("media.*"):
                    media.unlink(missing_ok=True)
                storage.remove_tree(sdir / "chunks")
                for f in [f for f in session.files if f.kind == "recording"]:
                    storage.remove_file(storage.upload_dir(sdir) / f.stored_name)
                    session.files.remove(f)
                for chunk in storage.upload_dir(sdir).glob("chunk_*"):
                    chunk.unlink(missing_ok=True)
                cleaned += bool(before)
            db.commit()
        if cleaned:
            logger.info("housekeeping removed media of %d lecture(s)", cleaned)
        return cleaned
