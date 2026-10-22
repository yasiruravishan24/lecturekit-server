"""Two lanes (SERVER_PLAN.md section 8, rule 5, extended): the main lane makes notes and quiz, one job at a time, first come
first served. A second lane does the extra work that follows a finished lecture (the pictures of the board and screen,
then the replay with blurred faces) at a low CPU priority, so it can never hold up anyone's notes.

Every unit of work is a `job` row first, then a queue entry. lecturekit is called outside any open database
transaction; the database is only touched in short sessions before and after the call."""
import contextlib
import json
import logging
import queue
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from lecturekit import api as lk
from lecturekit.errors import LectureKitError, MissingInputError

from . import notifications, storage
from .events import Hub, sync
from .errors import job_error
from .media import JOINED_NAME, LIVE_JOINED, join_live_chunks, join_recordings
from .models import Attempt, Job, LectureSession, LiveChunk, Note, Question, Shot, utcnow
from .settings import Settings

logger = logging.getLogger("lecturekit.server")
PROGRESS_EVERY = 1.0            # seconds: at most one progress write per second per job
BACKGROUND = ("visuals", "replay", "deeper")                   # the second lane
AUDIO_TYPES = {".m4a", ".mp3", ".wav", ".aac", ".ogg"}


class JobCancelled(BaseException):
    """Raised from the progress callback when the person pressed Cancel. A BaseException on purpose: the pipeline's own `except Exception` must not swallow it."""


class Worker:
    def __init__(self, settings: Settings, session_factory):
        self.settings, self.sf = settings, session_factory
        self.hub: Hub | None = None                                   # set by create_app: where progress and notifications are pushed
        self.q: "queue.Queue[int | None]" = queue.Queue()            # main lane: notes and quiz
        self.bg_q: "queue.Queue[int | None]" = queue.Queue()         # background lane: pictures, replay
        self.dp_q: "queue.Queue[int | None]" = queue.Queue()         # third lane: "learn more" (a web search and a few Claude calls, no CPU work)
        self.thread: threading.Thread | None = None
        self.bg_thread: threading.Thread | None = None
        self.dp_thread: threading.Thread | None = None
        self._running = False
        self._bg_running = False
        self._dp_running = False
        self._cancel_requested: set[int] = set()                     # jobs the person asked to stop (checked at every progress report)
        self._store_lock = threading.Lock()                           # pictures and "learn more" both add quiz questions: one at a time
        self._resume: list[tuple[int, str | None]] = []               # background jobs of the last run, queued again at start

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, args=(self.q, False), name="lecturekit-worker", daemon=True)
            self.thread.start()
            self.bg_thread = threading.Thread(target=self._loop, args=(self.bg_q, True), name="lecturekit-background", daemon=True)
            self.bg_thread.start()
            self.dp_thread = threading.Thread(target=self._loop, args=(self.dp_q, True), name="lecturekit-learn-more", daemon=True)
            self.dp_thread.start()
            for job_id, lane in self._resume:
                self.submit(job_id, background=True, lane=lane)
            self._resume = []

    def stop(self) -> None:
        for q_, th in ((self.q, self.thread), (self.bg_q, self.bg_thread), (self.dp_q, self.dp_thread)):
            if th is not None:
                q_.put(None)
                th.join(timeout=5)
        self.thread = self.bg_thread = self.dp_thread = None

    def submit(self, job_id: int, background: bool = False, lane: str | None = None) -> None:
        """lane "deeper": the "learn more" lane; otherwise background says the lane for pictures and replay, or the main one."""
        (self.dp_q if lane == "deeper" else self.bg_q if background else self.q).put(job_id)

    def request_cancel(self, job_id: int) -> None:
        """Ask a running job to stop: it stops at its next progress report (within seconds; a single long AI request finishes first)."""
        self._cancel_requested.add(job_id)

    def _is_cancelled(self, job_id: int) -> bool:
        return job_id in self._cancel_requested

    def queue_length(self) -> int:
        """Jobs of the main lane (waiting and running): what a person waiting for notes is behind."""
        return self.q.qsize() + (1 if self._running else 0)

    def background_length(self) -> int:
        return self.bg_q.qsize() + (1 if self._bg_running else 0) + self.dp_q.qsize() + (1 if self._dp_running else 0)

    def _loop(self, q_: "queue.Queue[int | None]", background: bool) -> None:
        while True:
            try:
                job_id = q_.get(timeout=self.settings.housekeeping_seconds)
            except queue.Empty:
                if not background:
                    self._safely(self.housekeeping)
                continue
            flag = "_dp_running" if q_ is self.dp_q else "_bg_running" if background else "_running"
            try:
                if job_id is None:
                    return
                setattr(self, flag, True)
                self._safely(self.run_job, job_id)
            finally:
                setattr(self, flag, False)
                q_.task_done()

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
            if self._is_cancelled(job_id):                  # the person pressed Cancel just as it was finishing: Cancel wins
                raise JobCancelled()
        except JobCancelled:
            logger.info("job %s (%s) cancelled", job_id, info["kind"])
            self._cancelled(info)
            return
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
        finally:
            self._cancel_requested.discard(job_id)

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
            elif job.kind == "visuals":
                session.visuals_state = "processing"
            elif job.kind == "replay":
                session.replay_state = "processing"
            elif job.kind == "deeper":
                session.deeper_state = "processing"
            sdir = storage.session_dir(self.settings, session.user_id, session.id)
            updir = storage.upload_dir(sdir)
            recordings = [updir / f.stored_name for f in session.files if f.kind == "recording" and not f.removed]
            live_chunks = []
            if session.source == "live" and session.live is not None and job.kind in BACKGROUND:
                existing = sorted(updir.glob(f"{LIVE_JOINED}.*"))     # the parts of a recording made with the extension, joined
                recordings = existing[:1]
                total = session.live.total or 0
                live_chunks = [c for k in range(total) for c in sorted(updir.glob(f"chunk_{k:05d}.*"))[:1]] if not existing else []
            if not recordings and not live_chunks and (replay := storage.find_replay(sdir)) is not None:
                recordings = [replay]                                # the original is gone: the replay has the same sound
            note = db.scalar(select(Note).where(Note.session_id == session.id))
            questions = [{"question": q.text, "source": q.source} for q in session.questions]
            info = {"job_id": job.id, "kind": job.kind, "arg": job.arg, "session_id": session.id,
                    "user_id": session.user_id, "title": session.title, "sdir": sdir, "recordings": recordings,
                    "slides": [updir / f.stored_name for f in session.files if f.kind == "slides"], "live_chunks": live_chunks,
                    "chunk_seconds": session.live.chunk_seconds if session.live else None,
                    "overlap": session.live.overlap if session.live else None,
                    "config": session.config, "source": session.source, "questions": questions,
                    "originals_gone": not any(f.kind == "recording" and not f.removed for f in session.files),
                    "notes": ({"title": note.title, "full_notes": json.loads(note.full_notes_json),
                               "short_notes": json.loads(note.short_notes_json)} if note is not None else None)}
            if job.kind == "chunk":
                info["chunk_file"] = next(iter(sorted(updir.glob(f"chunk_{job.arg:05d}.*"))), None)
            db.commit()
        sync(self.hub, info["user_id"], "job_started", info["session_id"])
        return info

    def _execute(self, info: dict) -> dict:
        kind, cb = info["kind"], self._progress(info)
        slides = info["slides"] or None
        if kind in BACKGROUND:
            return self._execute_background(info, cb)
        if kind == "process":
            recording = info["recordings"][0] if info["recordings"] else None
            if len(info["recordings"]) > 1:                      # a lecture saved in several files: one recording for the pipeline
                cb("prepare", 1, "Joining the recordings")
                recording = join_recordings(info["recordings"], storage.upload_dir(info["sdir"]) / JOINED_NAME)
            return lk.process_session(recording, info["sdir"], slides=slides, title=info["title"],
                                      final=True, on_progress=cb)
        if kind == "chunk":
            if info["chunk_file"] is None:
                raise MissingInputError(f"chunk {info['arg']} was not found on the server")
            return lk.add_chunk(info["sdir"], info["arg"], info["chunk_file"], info["chunk_seconds"],
                                info["overlap"], title=info["title"])
        return lk.finish_chunked_session(info["sdir"], info["arg"], slides=slides, chunk_seconds=info["chunk_seconds"],
                                         overlap=info["overlap"], final=True, on_progress=cb)

    def _execute_background(self, info: dict, cb) -> dict:
        """The extra work after a lecture. Needs one recording file (a lecture saved in several files only gets a replay of its sound)."""
        sdir, recordings = info["sdir"], info["recordings"]
        if info["kind"] == "deeper":                                     # needs the notes only, no recording
            if not info["notes"]:
                raise MissingInputError("there are no notes to go deeper on")
            return lk.go_deeper(sdir, info["notes"], info["questions"], n_questions=self.settings.deeper_questions,
                                max_searches=self.settings.deeper_searches, final=True, config_name=info["config"],
                                on_progress=lambda stage, pct: cb(stage, pct, ""))
        if not recordings and info["live_chunks"]:                       # made with the extension: join the parts first (picture and sound)
            cb("prepare", 1, "")
            recordings = [join_live_chunks(info["live_chunks"], info["overlap"] or 0, storage.upload_dir(sdir))]
        if not recordings or not recordings[0].exists():
            raise MissingInputError("the recording is no longer on the server")
        if info["kind"] == "visuals":
            video = recordings[0]
            if len(recordings) != 1 or video.suffix.lower() in AUDIO_TYPES:
                return {"shots": [], "notes": None}              # no picture to look at
            vfile = sdir / "visuals.json"
            if info["originals_gone"] and vfile.exists():            # a retry after the replay was made: use the pictures read before
                found = {"shots": json.loads(vfile.read_text()).get("shots", [])}
            else:
                found = lk.extract_visuals(video, sdir, final=True, on_progress=lambda stage, pct: cb(stage, pct, ""))
            merged, extra = None, []
            if found["shots"] and info["notes"]:
                cb("merge", 93, "")
                merged = lk.refine_notes_with_visuals(sdir, info["notes"], final=True, config_name=info["config"])
                if self.settings.visual_questions > 0 and not any(q["source"] == "board" for q in info["questions"]):
                    cb("quiz", 97, "")
                    extra = lk.add_visual_questions(sdir, info["questions"], n=self.settings.visual_questions, final=True, config_name=info["config"])
            return {"shots": found["shots"], "notes": merged, "questions": extra}
        source = recordings[0]
        if len(recordings) > 1:                                  # several parts: the sound only (joined earlier)
            joined = storage.upload_dir(sdir) / JOINED_NAME
            source = joined if joined.exists() else join_recordings(recordings, joined)
        return lk.make_replay(source, storage.replay_dir(sdir), on_progress=lambda pct: cb("replay", pct, ""))

    def _announce(self, info: dict, ok: bool, message: str = "") -> None:
        """Tell the user's open clients that this job ended (they fetch the lists again), and leave a notification when it matters."""
        user_id, session_id, kind = info["user_id"], info["session_id"], info["kind"]
        sync(self.hub, user_id, "job_done" if ok else "job_failed", session_id)
        note = (("notes_ready" if ok else "failed") if kind in ("process", "finish") else "replay_ready" if kind == "replay" and ok
                else ("deeper_ready" if ok else "deeper_failed") if kind == "deeper" else None)
        if note is None:
            return
        with self.sf() as db:
            session = db.get(LectureSession, session_id)
            if session is not None:
                notifications.add(db, self.hub, user_id, session, note, f"Processing failed: {message}" if note in ("failed", "deeper_failed") and message else "")

    def _progress(self, info: dict):
        """lecturekit's on_progress(stage, percent, message) -> job row (and the open clients), throttled to one write per second."""
        job_id = info["job_id"]
        last = {"t": 0.0, "stage": None}

        def cb(stage: str, percent: int, message: str = "") -> None:
            if job_id in self._cancel_requested:
                raise JobCancelled()
            now = time.time()
            if stage == last["stage"] and now - last["t"] < PROGRESS_EVERY:
                return
            last["t"], last["stage"] = now, stage
            with self.sf() as db:
                job = db.get(Job, job_id)
                if job is not None:
                    job.stage, job.percent, job.message = stage, int(percent), message or ""
                    db.commit()
            if self.hub is not None:
                self.hub.publish(info["user_id"], {"type": "job", "session_id": info["session_id"], "kind": info["kind"],
                                                   "stage": stage, "percent": int(percent), "message": message or ""})
        return cb

    def _succeed(self, info: dict, result: dict) -> None:
        lock = self._store_lock if info["kind"] in ("visuals", "deeper") else contextlib.nullcontext()
        with lock, self.sf() as db:
            job = db.get(Job, info["job_id"])
            session = db.get(LectureSession, info["session_id"])
            if job is None:
                return
            if session is not None and info["kind"] == "chunk":
                row = db.scalar(select(LiveChunk).where(LiveChunk.live_id == session.live.id, LiveChunk.idx == info["arg"]))
                if row is not None:
                    row.state, row.seconds, row.error_message = "transcribed", result.get("seconds"), None
                job.stage, job.percent, job.message = "done", 100, ""
            elif session is not None and info["kind"] == "visuals":
                self._store_visuals(db, session, result)
                job.stage, job.percent, job.message = "done", 100, ""
            elif session is not None and info["kind"] == "replay":
                self._store_replay(db, session, result, info)
                job.stage, job.percent, job.message = "done", 100, ""
            elif session is not None and info["kind"] == "deeper":
                self._store_deeper(db, session, result)
                job.stage, job.percent, job.message = "done", 100, ""
            elif session is not None:
                self._store_result(db, session, result)
                session.deeper_state = "none"                                  # the notes were made anew: the old "go deeper" no longer fits
                storage.remove_file(storage.session_dir(self.settings, session.user_id, session.id) / "deeper.json")
                job.stage, job.percent, job.message = "done", 100, "finished"
            job.state, job.finished_at = "done", utcnow()
            follows: list[tuple[int, str | None]] = []
            if session is not None and info["kind"] in ("process", "finish"):
                follows = self._start_background(db, session)
            elif session is not None and info["kind"] == "visuals":
                follows = [(j, None) for j in [self._next(db, session, "visuals")] if j is not None]
            db.commit()
        for job_id, lane in follows:
            self.submit(job_id, background=True, lane=lane)
        self._announce(info, True)

    # ---- the lane for extra work ------------------------------------------------------------------
    @staticmethod
    def _enqueue(db, session: LectureSession, kind: str) -> int:
        job = Job(session_id=session.id, user_id=session.user_id, kind=kind)
        db.add(job)
        if kind == "visuals":
            session.visuals_state = "queued"
        elif kind == "deeper":
            session.deeper_state = "queued"
        else:
            session.replay_state = "queued"
        db.flush()
        return job.id

    def _has_media(self, session: LectureSession) -> bool:
        """There is a recording (or the parts of one made with the extension) to make pictures and a replay from."""
        if session.source not in ("upload", "live"):
            return False
        sdir = storage.session_dir(self.settings, session.user_id, session.id)
        has_parts = session.source == "live" and any(storage.upload_dir(sdir).glob("chunk_*"))
        return has_parts or any(f.kind == "recording" and not f.removed for f in session.files)

    def _auto_deeper(self, session: LectureSession) -> bool:
        return self.settings.deeper_auto and session.state == "done" and session.deeper_state in ("none", "failed")

    def _start_background(self, db, session: LectureSession) -> list[tuple[int, str | None]]:
        """Right after the notes, at the same time: the board pictures (then the replay follows) in the background lane, and "learn more"
        in its own lane. Returns [(job id, lane)]."""
        out: list[tuple[int, str | None]] = []
        if self.settings.visuals_enabled and (self._has_media(session) or (session.replay_state == "ready" and session.shot_count)):
            out.append((self._enqueue(db, session, "visuals"), None))
        if self._auto_deeper(session):
            out.append((self._enqueue(db, session, "deeper"), "deeper"))
        return out

    def _next(self, db, session: LectureSession, finished: str) -> int | None:
        """The job that follows the pictures (done or failed): the replay."""
        started = self.settings.visuals_enabled or session.visuals_state in ("done", "failed", "skipped")
        if finished == "visuals" and started and self._has_media(session) and session.replay_state != "ready":
            return self._enqueue(db, session, "replay")
        return None

    def start_deeper(self, db, session: LectureSession) -> int:
        """Queue "Go deeper" for a finished lecture (the second lane). Returns the job id."""
        job_id = self._enqueue(db, session, "deeper")
        db.commit()
        self.submit(job_id, background=True, lane="deeper")
        return job_id

    def start_background(self, db, session: LectureSession) -> int | None:
        """Used by the route that makes the replay on request: queue the extra work now. Returns the first job's id."""
        job_id = self._enqueue(db, session, "visuals")
        db.commit()
        self.submit(job_id, background=True)
        return job_id

    @staticmethod
    def _store_visuals(db, session: LectureSession, result: dict) -> None:
        for old in db.scalars(select(Shot).where(Shot.session_id == session.id)).all():
            db.delete(old)
        db.flush()
        for s in result["shots"]:
            db.add(Shot(session_id=session.id, t=float(s["t"]), kind=s["kind"], text=s["text"], description=s["description"], file=s["file"]))
        session.shot_count, session.visuals_state = len(result["shots"]), "done"
        merged = result.get("notes")
        note = db.scalar(select(Note).where(Note.session_id == session.id))
        if merged and note is not None:
            note.title = merged.get("title", note.title)
            note.full_notes_json = json.dumps(merged.get("full_notes", []))
            note.short_notes_json = json.dumps(merged.get("short_notes", []))
            stats = json.loads(note.stats_json or "{}")
            stats["visuals"] = len(result["shots"])
            note.stats_json = json.dumps(stats)
        position = max([q.position for q in session.questions] or [0])
        for q in result.get("questions") or []:                      # extra questions go after the old ones: attempts already made stay valid
            position += 1
            db.add(Question(session_id=session.id, position=position, text=q["question"], options_json=json.dumps(q["options"]),
                            answer_index=int(q["answer_index"]), bloom_level=q.get("bloom_level", ""), explanation=q.get("explanation", ""),
                            source="board"))

    @staticmethod
    def _store_deeper(db, session: LectureSession, result: dict) -> None:
        """Go deeper is done: the file is on disk (deeper.json); its extra questions go after the old ones, so attempts already made stay valid."""
        session.deeper_state = "done"
        position = max([q.position for q in session.questions] or [0])
        for q in result.get("questions") or []:
            position += 1
            db.add(Question(session_id=session.id, position=position, text=q["question"], options_json=json.dumps(q["options"]),
                            answer_index=int(q["answer_index"]), bloom_level=q.get("bloom_level", ""), explanation=q.get("explanation", ""),
                            source="extra"))

    def _store_replay(self, db, session: LectureSession, result: dict, info: dict) -> None:
        """The replay exists: keep it, and delete the unblurred original (and the copies made for the pipeline)."""
        session.replay_state, session.replay_kind, session.replay_bytes = "ready", result["kind"], int(result["bytes"])
        sdir = info["sdir"]
        for f in session.files:
            if f.kind == "recording" and not f.removed:
                storage.remove_file(storage.upload_dir(sdir) / f.stored_name)
                f.removed, f.bytes = True, 0
        storage.remove_file(storage.upload_dir(sdir) / JOINED_NAME)
        for leftover in [*storage.upload_dir(sdir).glob(f"{LIVE_JOINED}.*"), *storage.upload_dir(sdir).glob("chunk_*")]:
            leftover.unlink(missing_ok=True)                           # the parts of a recording made with the extension
        for media in sdir.glob("media.*"):
            media.unlink(missing_ok=True)

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

    @staticmethod
    def reset_extra(session: LectureSession, kind: str) -> None:
        """After a cancelled extra job: the lecture and its notes are untouched, the extra can be made again."""
        if kind == "visuals":
            session.visuals_state = "skipped"
        elif kind == "replay":
            session.replay_state = "none"
        elif kind == "deeper":
            session.deeper_state = "none"

    def _cancelled(self, info: dict) -> None:
        """The person pressed Cancel. Notes in the making: the lecture and its recording are deleted. An extra (pictures, replay, learn more): only it stops."""
        kind = info["kind"]
        with self.sf() as db:
            job = db.get(Job, info["job_id"])
            session = db.get(LectureSession, info["session_id"])
            if job is not None:
                job.state, job.finished_at, job.stage = "cancelled", utcnow(), "cancelled"       # kept (not counted against the daily limit)
            if session is not None:
                if kind in ("process", "finish"):
                    db.delete(session)
                else:
                    self.reset_extra(session, kind)
            db.commit()
        if kind in ("process", "finish"):
            storage.remove_tree(storage.session_dir(self.settings, info["user_id"], info["session_id"]))
        sync(self.hub, info["user_id"], "cancelled", info["session_id"])

    def _fail(self, info: dict, code: str, message: str) -> None:
        with self.sf() as db:
            job = db.get(Job, info["job_id"])
            session = db.get(LectureSession, info["session_id"])
            if job is not None:
                job.state, job.finished_at, job.error_code, job.error_message = "failed", utcnow(), code, message
            follow = None
            if session is not None:
                if info["kind"] == "chunk":
                    row = db.scalar(select(LiveChunk).where(LiveChunk.live_id == session.live.id, LiveChunk.idx == info["arg"]))
                    if row is not None:
                        row.state, row.error_message = "failed", message
                elif info["kind"] == "visuals":
                    session.visuals_state = "failed"                 # the notes stay as they were; the rest still gets made
                    follow = self._next(db, session, "visuals")
                elif info["kind"] == "replay":
                    session.replay_state = "failed"                  # the original stays, so it can be tried again
                elif info["kind"] == "deeper":
                    session.deeper_state = "failed"                  # the notes are not touched; it can be tried again
                else:
                    session.state = "failed"
            db.commit()
        if follow is not None:
            self.submit(follow, background=True)
        self._announce(info, False, message)

    # ---- startup recovery and housekeeping -----------------------------------------------------
    def recover(self) -> int:
        """Jobs left `queued` or `processing` by a previous run are failed: the user retries (SERVER_PLAN.md NF3)."""
        with self.sf() as db:
            stale = db.scalars(select(Job).where(Job.state.in_(("queued", "processing")))).all()
            for job in [j for j in stale if j.kind in BACKGROUND]:     # extra work is simply started again
                job.state, job.stage, job.percent = "queued", "queued", 0
                self._resume.append((job.id, "deeper" if job.kind == "deeper" else None))
            stale = [j for j in stale if j.kind not in BACKGROUND]
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
        """Remove the large files of lectures finished more than KEEP_MEDIA_DAYS ago, but only for lectures that have no replay
        (older ones, and live recordings): a replay is kept until its owner deletes the lecture.

        Notes, quiz and transcript stay. Returns how many sessions were cleaned."""
        cutoff = utcnow() - timedelta(days=self.settings.keep_media_days)
        cleaned = 0
        with self.sf() as db:
            for session in db.scalars(select(LectureSession).where(LectureSession.state == "done", LectureSession.replay_state == "none")).all():
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
                storage.remove_file(storage.upload_dir(sdir) / JOINED_NAME)
                for chunk in storage.upload_dir(sdir).glob("chunk_*"):
                    chunk.unlink(missing_ok=True)
                cleaned += bool(before)
            db.commit()
        if cleaned:
            logger.info("housekeeping removed media of %d lecture(s)", cleaned)
        return cleaned
