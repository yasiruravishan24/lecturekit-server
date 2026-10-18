"""Joining the parts of one lecture recording (a lecture saved in several files)."""
import subprocess
from pathlib import Path

from lecturekit.errors import ExternalToolError

JOINED_NAME = "joined.m4a"


def join_recordings(parts: list[Path], out: Path, timeout: int = 3600) -> Path:
    """Join the sound of the parts, in the given order, into one mono AAC file (the pipeline only needs the sound).

    Every part is first converted to the same sample rate and channel layout, so parts from different
    programs (a phone, Zoom, a laptop) can be mixed."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in parts:
        cmd += ["-i", str(p)]
    prep = "".join(f"[{i}:a:0]aresample=16000,aformat=channel_layouts=mono[a{i}];" for i in range(len(parts)))
    joined = "".join(f"[a{i}]" for i in range(len(parts)))
    cmd += ["-filter_complex", f"{prep}{joined}concat=n={len(parts)}:v=0:a=1[out]", "-map", "[out]",
            "-c:a", "aac", "-b:a", "64k", str(out)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ExternalToolError("ffmpeg is not installed") from None
    except subprocess.TimeoutExpired:
        raise ExternalToolError("ffmpeg took too long to join the recordings") from None
    if done.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise ExternalToolError(f"ffmpeg could not join the recordings (does every part have sound?): {done.stderr[-300:]}")
    return out
