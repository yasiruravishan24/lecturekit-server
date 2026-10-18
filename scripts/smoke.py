"""Drive a running server over HTTP, like the web app or the extension would (manual acceptance, SERVER_PLAN.md section 12).

  whole file:  python scripts/smoke.py --url http://localhost:8000 --recording talk.mp4 [--slides slides.pdf]
  live chunks: python scripts/smoke.py --url ... --recording talk.mp4 --live [--end 600] [--slides slides.pdf]

It registers the user if needed (on a real server, enter the emailed code first, or run: python -m server.admin verify-email <email>), creates a course, module and session, uploads (or streams chunks), waits for the job while
printing stage and percent, reads the notes and the quiz, submits an attempt, downloads the export and deletes the session.
The live mode needs lecturekit on the Python path (PYTHONPATH=../lecturekit) and ffmpeg, to cut the chunks.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--email", default="smoke@example.com")
    ap.add_argument("--password", default="smoke-test-password")
    ap.add_argument("--recording", required=True)
    ap.add_argument("--slides", nargs="*", default=[])
    ap.add_argument("--live", action="store_true", help="send 5-minute chunks instead of one file")
    ap.add_argument("--end", type=float, default=None, help="live mode: use only the first END seconds")
    ap.add_argument("--keep", action="store_true", help="do not delete the session at the end")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed certificate (a test on localhost)")
    a = ap.parse_args()

    c = httpx.Client(base_url=a.url.rstrip("/") + "/api", timeout=600, verify=not a.insecure)
    r = c.post("/auth/register", json={"full_name": "Smoke Test", "email": a.email, "password": a.password, "accept_terms": True})
    print("register:", r.status_code, "(409 = already registered)" if r.status_code == 409 else "")
    r = c.post("/auth/login", json={"email": a.email, "password": a.password})
    if r.status_code == 403:
        raise SystemExit("Confirm the email first: POST /auth/verify-email with the code from the email, "
                         f"or run: python -m server.admin verify-email {a.email}")
    r.raise_for_status()
    h = {"Authorization": "Bearer " + r.json()["token"]}
    course = c.post("/courses", json={"name": "Smoke test"}, headers=h).json()
    module = c.post(f"/courses/{course['id']}/modules", json={"name": "Week 1"}, headers=h).json()
    sid = c.post(f"/modules/{module['id']}/sessions", json={"title": Path(a.recording).stem}, headers=h).json()["id"]
    slides = [("slides", (Path(p).name, open(p, "rb"))) for p in a.slides]
    t0 = time.time()

    if a.live:
        from lecturekit import chunks as lk_chunks      # only to cut the recording into the contract's pieces
        contract = c.post(f"/sessions/{sid}/live", headers=h).json()
        print("live contract:", contract)
        parts = lk_chunks.split_recording(a.recording, Path(tempfile.mkdtemp()), contract["chunk_seconds"], contract["overlap"], a.end)
        for k, path in parts:
            r = c.post(f"/sessions/{sid}/live/chunks", params={"index": k}, headers=h, files={"chunk": (path.name, open(path, "rb"))})
            print(f"chunk {k}:", r.status_code, r.json())
        t_stop = time.time()
        print("status:", c.get(f"/sessions/{sid}/live", params={"total": len(parts)}, headers=h).json())
        r = c.post(f"/sessions/{sid}/live/finish", params={"total": len(parts)}, headers=h, files=slides)
    else:
        r = c.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", (Path(a.recording).name, open(a.recording, "rb")))] + slides)
        print("upload:", r.status_code, [f["kind"] for f in r.json()["files"]])
        t_stop = time.time()
        r = c.post(f"/sessions/{sid}/process", headers=h)
    r.raise_for_status()
    job_id, last = r.json()["job_id"], None
    while True:
        job = c.get(f"/jobs/{job_id}", headers=h).json()
        line = (job["state"], job["stage"], job["percent"])
        if line != last:
            print(f"  {time.time() - t_stop:6.1f} s after the last upload: {line[0]:10} {line[1]:11} {line[2]:3}%  {job['message']}")
            last = line
        if job["state"] in ("done", "failed"):
            break
        time.sleep(2)
    if job["state"] == "failed":
        print("FAILED:", job["error_code"], job["error_message"])
        return 1
    print(f"done: {time.time() - t_stop:.0f} s after the last upload, {time.time() - t0:.0f} s in total")

    notes = c.get(f"/sessions/{sid}/notes", headers=h).json()
    quiz = c.get(f"/sessions/{sid}/quiz", headers=h).json()["questions"]
    print(f"notes: {notes['title']!r}, {len(notes['full_notes'])} sections, config {notes['config']}, model {notes['model']}")
    print(f"quiz: {len(quiz)} questions; any answer in the quiz text? {'answer' in str(quiz) or 'explanation' in str(quiz)}")
    att = c.post(f"/sessions/{sid}/quiz/attempts", json={"answers": [0] * len(quiz)}, headers=h).json()
    print(f"attempt (all answers = A): score {att['score']}/{att['total']}")
    print("export.md:", len(c.get(f"/sessions/{sid}/export.md", headers=h).text), "characters")
    if not a.keep:
        print("delete session:", c.delete(f"/sessions/{sid}", headers=h).status_code)
        c.delete(f"/modules/{module['id']}", headers=h)
        c.delete(f"/courses/{course['id']}", headers=h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
