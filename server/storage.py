"""Files on disk (SERVER_PLAN.md sections 4 and 9). Paths are built from ids, never from text the client sent."""
import shutil
from pathlib import Path

from fastapi import UploadFile

from .errors import ApiError
from .settings import Settings

BLOCK = 1024 * 1024
# What lecturekit makes from an upload; removed when a new recording or new slides arrive so nothing stale is reused.
DERIVED = ("transcript.json", "slides.json", "slides.pdf", "meta.json")


def session_dir(settings: Settings, user_id: int, session_id: int) -> Path:
    return settings.users_dir / str(user_id) / "sessions" / str(session_id)


def upload_dir(sdir: Path) -> Path:
    return sdir / "upload"


def replay_dir(sdir: Path) -> Path:
    return sdir / "replay"


def shots_dir(sdir: Path) -> Path:
    return sdir / "shots"


def find_replay(sdir: Path) -> Path | None:
    for name in ("replay.mp4", "replay.m4a"):
        f = replay_dir(sdir) / name
        if f.exists():
            return f
    return None


def check_extension(filename: str | None, allowed: set[str], what: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in allowed:
        raise ApiError(422, "validation", f"Unsupported {what} type {ext or '(none)'!r}; use one of {sorted(allowed)}")
    return ext


def save_upload(upload: UploadFile, dest: Path, limit_mb: int) -> int:
    """Copy an upload to `dest` in 1 MB blocks. On overflow the partial file is deleted and 413 is raised."""
    limit = limit_mb * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as out:
            while block := upload.file.read(BLOCK):
                size += len(block)
                if size > limit:
                    raise ApiError(413, "too_large", f"File too large (limit {limit_mb} MB)")
                out.write(block)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ApiError(422, "validation", "The file is empty")
    return size


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def clear_derived(sdir: Path) -> None:
    """Forget everything lecturekit made from the old upload (transcript, slide text, outputs, chunks, media copy)."""
    for name in DERIVED:
        remove_file(sdir / name)
    for media in sdir.glob("media.*"):
        media.unlink(missing_ok=True)
    remove_tree(sdir / "out")
    remove_tree(sdir / "chunks")
