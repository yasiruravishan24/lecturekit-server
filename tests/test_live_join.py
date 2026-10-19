"""join_live_chunks with the real ffmpeg: the overlap of every part but the first is cut off, picture and sound stay in step."""
import shutil
import subprocess

import pytest

from server.media import join_live_chunks

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def make_part(path, seconds, video=True):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}", "-c:v", "libvpx", "-b:v", "200k"]
    subprocess.run(cmd + ["-c:a", "libopus", str(path)], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return float(out.strip())


def test_video_parts_become_one_video_without_the_overlap(tmp_path):
    parts = [tmp_path / "chunk_00000.webm", tmp_path / "chunk_00001.webm", tmp_path / "chunk_00002.webm"]
    for p, secs in zip(parts, (10, 15, 12)):                  # part k covers [k*10-5, (k+1)*10]: 10 s, 15 s, 15 s (here 12: Stop came early)
        make_part(p, secs)
    out = join_live_chunks(parts, 5, tmp_path)
    assert out.name == "live_joined.mp4"
    assert 26 < duration(out) < 28.5                          # 10 + (15-5) + (12-5) = 27 s of lecture
    assert not (tmp_path / ".live_parts").exists()            # the working files are gone


def test_sound_only_parts_become_one_sound_file(tmp_path):
    parts = [tmp_path / "chunk_00000.webm", tmp_path / "chunk_00001.webm"]
    for p, secs in zip(parts, (10, 15)):
        make_part(p, secs, video=False)
    out = join_live_chunks(parts, 5, tmp_path)
    assert out.name == "live_joined.m4a"
    assert 19 < duration(out) < 21.5                          # 10 + 10 s


def test_no_parts_is_an_error(tmp_path):
    from lecturekit.errors import ExternalToolError
    with pytest.raises(ExternalToolError):
        join_live_chunks([], 5, tmp_path)
