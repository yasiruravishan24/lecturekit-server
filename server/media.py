"""Joining the parts of one lecture recording (a lecture saved in several files)."""
import shutil
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


LIVE_JOINED = "live_joined"                       # stem of the file made from the parts of a recording made with the extension


def _has_video(path: Path) -> bool:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return "video" in out


def join_live_chunks(chunks: list[Path], overlap: int, out_dir: Path, timeout: int = 7200) -> Path:
    """One file from the parts of a recording made with the extension: picture and sound when the parts have a picture, else sound.

    Part k starts `overlap` seconds before the end of part k-1, so the first `overlap` seconds of every part but the first are
    cut off. Every part is first made into the same size and speed (a browser tab can change size), then the pieces are joined."""
    chunks = list(chunks)
    if not chunks:
        raise ExternalToolError("there are no recorded parts to join")
    video = _has_video(chunks[0])
    ext = ".mp4" if video else ".m4a"
    out = out_dir / f"{LIVE_JOINED}{ext}"
    work = out_dir / ".live_parts"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        parts = []
        for k, chunk in enumerate(chunks):
            part = work / f"p{k:05d}{ext}"
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(chunk)]
            if k >= 1:
                cmd += ["-ss", str(overlap)]                    # after -i: cut exactly, not at a key frame
            if video:
                cmd += ["-vf", "fps=10,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1"]
            else:
                cmd += ["-vn", "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1"]
            run = subprocess.run(cmd + [str(part)], capture_output=True, text=True, timeout=timeout)
            if run.returncode != 0 or not part.exists():
                raise ExternalToolError(f"ffmpeg could not read part {k + 1} of the recording: {run.stderr[-300:]}")
            parts.append(part)
        listing = work / "list.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
        run = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)],
                             capture_output=True, text=True, timeout=timeout)
        if run.returncode != 0 or not out.exists():
            out.unlink(missing_ok=True)
            raise ExternalToolError(f"ffmpeg could not join the parts of the recording: {run.stderr[-300:]}")
        return out
    except FileNotFoundError:
        raise ExternalToolError("ffmpeg is not installed") from None
    except subprocess.TimeoutExpired:
        raise ExternalToolError("ffmpeg took too long to join the parts of the recording") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
