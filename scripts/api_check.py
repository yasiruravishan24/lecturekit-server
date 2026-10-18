"""Check every feature of a RUNNING server over real HTTP (local, in Docker, or deployed). Prints PASS / FAIL per check.

  without AI (no Whisper or Claude calls, nothing to pay):
      python scripts/api_check.py --url https://localhost:8443 --insecure --verify-cmd "docker compose exec -T app python -m server.admin verify-email {email}"
  everything, with the real pipeline on a short clip (about 4 minutes, a few cents of Claude credit):
      python scripts/api_check.py --url ... --recording talk.mp4 --slides slides.pdf [--clip-seconds 330]

The emailed code never appears in an API reply, so every test user is confirmed by --verify-cmd: a shell command in which {email} is replaced
by the user's address (run from the project folder; for a remote server use ssh ... 'cd /opt/lecturekit && docker compose exec -T app python -m server.admin verify-email {email}').
It creates throw-away users and deletes them at the end. It needs ffmpeg only for the AI part (to cut the clip and the chunks).
Covers: health, auth (sign-up details, email verification, Google login, duplicates, lockout, tokens), courses/modules/sessions (CRUD, validation, isolation
between two users, cascade delete), uploads (types, replace), errors from the pipeline, retry, daily limit, whole-file processing,
notes, quiz (no answers before an attempt, scoring, history, validation), export, live chunks (contract, missing chunks, finish),
and deleting the account.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PASSWORD = "an-api-check-password"
RESULTS: list[tuple[str, str, str]] = []          # (status, name, detail)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(("PASS" if ok else "FAIL", name, "" if ok else detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   <- {detail}"))
    return ok


def skip(name: str, why: str) -> None:
    RESULTS.append(("SKIP", name, why))
    print(f"  skip  {name}   ({why})")


def section(title: str) -> None:
    print(f"\n{title}")


def err(r: httpx.Response) -> tuple[str, str]:
    try:
        e = r.json()["error"]
        return e["code"], e["message"]
    except Exception:
        return "?", r.text[:120]


class Api:
    def __init__(self, base: str, insecure: bool):
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=900, verify=not insecure)

    def __call__(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.c.request(method, ("/api" + path) if not path.startswith("/health") else path, headers=headers, **kw)


def register_body(email: str, **over) -> dict:
    return {"full_name": "Check User", "email": email, "password": PASSWORD, "accept_terms": True, **over}


VERIFY_CMD = ""


def confirm_by_command(email: str) -> None:
    """Confirm an address without the emailed code (the code is only in the email)."""
    done = subprocess.run(VERIFY_CMD.replace("{email}", email), shell=True, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"--verify-cmd failed for {email}: {done.stdout[-200:]} {done.stderr[-200:]}"


def make_user(api: Api, label: str) -> dict:
    email = f"{label}{int(time.time() * 1000)}@example.com"
    r = api("POST", "/auth/register", json=register_body(email))
    assert r.status_code == 201, f"cannot create a test user: {r.status_code} {r.text}"
    confirm_by_command(email)
    token = api("POST", "/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
    return {"email": email, "token": token}


def new_session(api: Api, token: str, title: str = "Check lecture") -> int:
    c = api("POST", "/courses", token, json={"name": "API check"}).json()
    m = api("POST", f"/courses/{c['id']}/modules", token, json={"name": "Week 1"}).json()
    return api("POST", f"/modules/{m['id']}/sessions", token, json={"title": title}).json()["id"]


def wait_job(api: Api, token: str, job_id: int, timeout: int = 900, show: bool = False) -> tuple[dict, list[str]]:
    end, stages, job = time.time() + timeout, [], {}
    while time.time() < end:
        job = api("GET", f"/jobs/{job_id}", token).json()
        if job["stage"] not in stages and job["state"] == "processing":
            stages.append(job["stage"])
            if show:
                print(f"        {job['stage']:11} {job['percent']:3}%")
        if job["state"] in ("done", "failed"):
            return job, stages
        time.sleep(1.5)
    return job, stages


def garbage_upload(api: Api, token: str, sid: int) -> httpx.Response:
    return api("POST", f"/sessions/{sid}/upload", token, files=[("recording", ("broken.mp4", b"this is not a video", "video/mp4"))])


def run(a) -> int:
    api = Api(a.url, a.insecure)
    key_present = False

    section("1. Health and basic behaviour")
    r = api("GET", "/health")
    h = r.json() if r.status_code == 200 else {}
    check("GET /health answers 200 with ok, checks, models, queue_length", r.status_code == 200 and {"ok", "checks", "models", "queue_length"} <= set(h), r.text[:100])
    check("/health shows no secret or path", not re.search(r"JWT|data_dir|log_dir|sk-ant", r.text, re.I))
    key_present = bool(h.get("checks", {}).get("ANTHROPIC_API_KEY"))
    print(f"        (the server {'has' if key_present else 'does NOT have'} an Anthropic API key; ffmpeg {h.get('checks', {}).get('ffmpeg')}, tesseract {h.get('checks', {}).get('tesseract')}, libreoffice {h.get('checks', {}).get('libreoffice')})")
    check("GET /api/health works too", api.c.get("/api/health").status_code == 200)
    r = api("GET", "/nope")
    check("unknown route -> 404 with the standard error shape", r.status_code == 404 and err(r)[0] == "not_found", r.text[:100])
    r = api("GET", "/courses")
    check("no token -> 401 'Please log in'", r.status_code == 401 and err(r) == ("unauthorized", "Please log in"), r.text[:100])

    section("2. Accounts")
    email = f"checkA{int(time.time() * 1000)}@example.com"
    r = api("POST", "/auth/register", json=register_body(email, password="short"))
    check("register with a short password -> 422", r.status_code == 422 and err(r)[0] == "validation", r.text[:100])
    r = api("POST", "/auth/register", json=register_body("not-an-email"))
    check("register with a bad email -> 422", r.status_code == 422)
    r = api("POST", "/auth/register", json=register_body(email, accept_terms=False))
    check("register without accepting the terms -> 422", r.status_code == 422 and err(r)[0] == "validation", r.text[:100])
    r = api("POST", "/auth/register", json={"email": email, "password": PASSWORD, "accept_terms": True})
    check("register without a full name -> 422", r.status_code == 422)
    r = api("POST", "/auth/register", json=register_body(email.upper()))
    user = r.json().get("user", {})
    check("register ok -> 201, email in lower case, not verified yet, no password in the reply",
          r.status_code == 201 and user.get("email") == email.lower() and user.get("email_verified") is False and user.get("full_name") == "Check User" and r.json().get("verification_sent") is True and "hash" not in r.text, r.text[:100])
    check("the reply never contains the email code", "code" not in r.text and "token" not in r.text.lower(), r.text[:100])
    r = api("POST", "/auth/login", json={"email": email, "password": PASSWORD})
    check("login before the email is confirmed -> 403 email_not_verified", r.status_code == 403 and err(r)[0] == "email_not_verified", r.text[:100])
    r = api("POST", "/auth/verify-email", json={"email": email, "code": "000000"})
    check("a wrong code -> 400 invalid_code", r.status_code == 400 and err(r)[0] == "invalid_code", r.text[:100])
    r = api("POST", "/auth/resend-verification", json={"email": "nobody-here@example.com"})
    check("resend for an unknown email answers the same 200 (no account is revealed)", r.status_code == 200 and r.json() == {"sent": True}, r.text[:100])
    confirm_by_command(email)
    r = api("POST", "/auth/login", json={"email": email, "password": PASSWORD})
    check("after the email is confirmed, login works", r.status_code == 200, r.text[:100])
    r = api("POST", "/auth/register", json=register_body(email))
    check("register the same email again -> 409 email_taken", r.status_code == 409 and err(r)[0] == "email_taken", r.text[:100])
    r = api("GET", "/auth/config")
    check("GET /auth/config tells whether Google login is on", r.status_code == 200 and set(r.json()) == {"google_enabled", "google_client_id"}, r.text[:100])
    r = api("POST", "/auth/google", json={"id_token": "not-a-real-token", "accept_terms": True})
    check("a fake Google token is refused (401 google_invalid, or 503 when Google login is off)", r.status_code in (401, 503) and err(r)[0] in ("google_invalid", "google_disabled"), r.text[:100])
    bad = api("POST", "/auth/login", json={"email": email, "password": "a wrong password"})
    unknown = api("POST", "/auth/login", json={"email": "nobody-here@example.com", "password": PASSWORD})
    check("wrong password and unknown email give the same 401", bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json(), f"{bad.text[:60]} / {unknown.text[:60]}")
    r = api("POST", "/auth/login", json={"email": email, "password": PASSWORD})
    ok = r.status_code == 200 and {"token", "expires_at", "user"} <= set(r.json())
    check("login -> token, expires_at (about 7 days), user", ok, r.text[:100])
    tokenA = r.json()["token"]
    check("GET /auth/me with the token", api("GET", "/auth/me", tokenA).json()["user"]["email"] == email.lower())
    check("a tampered token -> 401", api("GET", "/auth/me", tokenA[:-4] + "AAAA").status_code == 401)
    lock = f"lock{int(time.time() * 1000)}@example.com"
    codes = [api("POST", "/auth/login", json={"email": lock, "password": "x" * 9}).status_code for _ in range(5)]
    r = api("POST", "/auth/login", json={"email": lock, "password": "x" * 9})
    check("5 failed logins, then the 6th is locked out -> 429 too_many_attempts", codes == [401] * 5 and r.status_code == 429 and err(r)[0] == "too_many_attempts", f"{codes} then {r.status_code}")

    section("3. Courses, modules, lectures")
    r = api("POST", "/courses", tokenA, json={"name": "  Intro to CS  "})
    cid = r.json()["id"]
    check("create a course (name trimmed) -> 201", r.status_code == 201 and r.json()["name"] == "Intro to CS")
    check("empty and too long names -> 422", all(api("POST", "/courses", tokenA, json={"name": n}).status_code == 422 for n in ("", "   ", "x" * 121)))
    check("rename a course", api("PATCH", f"/courses/{cid}", tokenA, json={"name": "CS 101"}).json()["name"] == "CS 101")
    mid = api("POST", f"/courses/{cid}/modules", tokenA, json={"name": "Week 1"}).json()["id"]
    check("create, list and rename a module", api("PATCH", f"/modules/{mid}", tokenA, json={"name": "Week 2"}).json()["name"] == "Week 2" and [m["name"] for m in api("GET", f"/courses/{cid}/modules", tokenA).json()] == ["Week 2"])
    r = api("POST", f"/modules/{mid}/sessions", tokenA, json={"title": "Loops"})
    sid = r.json()["id"]
    s = r.json()
    check("create a lecture: state empty, no files, no notes", r.status_code == 201 and s["state"] == "empty" and s["files"] == [] and s["has_notes"] is False and s["latest_job"] is None, r.text[:120])
    check("rename and list lectures", api("PATCH", f"/sessions/{sid}", tokenA, json={"title": "Loops and lists"}).json()["title"] == "Loops and lists" and len(api("GET", f"/modules/{mid}/sessions", tokenA).json()) == 1)

    section("4. Isolation: a second user sees and changes nothing of the first")
    B = make_user(api, "checkB")
    probes = [("GET", f"/courses/{cid}/modules"), ("PATCH", f"/courses/{cid}"), ("DELETE", f"/courses/{cid}"), ("PATCH", f"/modules/{mid}"),
              ("GET", f"/modules/{mid}/sessions"), ("POST", f"/modules/{mid}/sessions"), ("GET", f"/sessions/{sid}"), ("PATCH", f"/sessions/{sid}"),
              ("DELETE", f"/sessions/{sid}"), ("GET", f"/sessions/{sid}/notes"), ("GET", f"/sessions/{sid}/quiz"), ("POST", f"/sessions/{sid}/quiz/attempts"),
              ("GET", f"/sessions/{sid}/export.md"), ("POST", f"/sessions/{sid}/process"), ("POST", f"/sessions/{sid}/upload"),
              ("POST", f"/sessions/{sid}/live"), ("GET", f"/sessions/{sid}/live"), ("GET", "/jobs/1")]
    bodies = {"PATCH": {"name": "hacked", "title": "hacked"}, "POST": {"name": "x", "title": "x", "answers": []}}
    results = [api(m, p, B["token"], json=bodies.get(m)) if m in bodies else api(m, p, B["token"]) for m, p in probes]
    wrong = [f"{m} {p} -> {r.status_code}" for (m, p), r in zip(probes, results) if r.status_code != 404]
    check(f"user B gets 404 on all {len(probes)} probes of user A's data", not wrong, "; ".join(wrong[:3]))
    check("user B's course list is empty; user A's data is untouched", api("GET", "/courses", B["token"]).json() == [] and api("GET", f"/sessions/{sid}", tokenA).json()["title"] == "Loops and lists")

    section("5. Uploads")
    r = api("POST", f"/sessions/{sid}/process", tokenA)
    check("process with no recording -> 422 missing_input", r.status_code == 422 and err(r)[0] == "missing_input", r.text[:100])
    r = api("POST", f"/sessions/{sid}/upload", tokenA, files=[("recording", ("notes.txt", b"x", "text/plain"))])
    check("a .txt recording -> 422 with the allowed types listed", r.status_code == 422 and ".mp4" in err(r)[1], r.text[:100])
    r = api("POST", f"/sessions/{sid}/upload", tokenA, files=[("recording", ("ok.mp4", b"x", "video/mp4")), ("slides", ("bad.exe", b"x", "application/octet-stream"))])
    check("a bad slide type rejects the whole upload and keeps nothing", r.status_code == 422 and api("GET", f"/sessions/{sid}", tokenA).json()["files"] == [])
    r = api("POST", f"/sessions/{sid}/upload", tokenA)
    check("an upload with no files -> 422", r.status_code == 422)
    r = api("POST", f"/sessions/{sid}/upload", tokenA, files=[("recording", ("../../evil name.MP4", b"12345", "video/mp4")), ("slides", ("a.pdf", b"%PDF", "application/pdf")), ("slides", ("b.pptx", b"ppt", "application/octet-stream"))])
    files = r.json().get("files", []) if r.status_code == 200 else []
    check("upload a recording and 2 slide files -> state uploaded, files in order", r.status_code == 200 and r.json()["state"] == "uploaded" and [f["kind"] for f in files] == ["recording", "slides", "slides"] and r.json()["has_slides"], r.text[:120])
    r = api("POST", f"/sessions/{sid}/upload", tokenA, files=[("recording", ("two.m4a", b"22", "audio/mp4"))])
    names = [f["original_name"] for f in r.json()["files"] if f["kind"] == "recording"]
    check("a new recording replaces the old one; slides stay", names == ["two.m4a"] and sum(f["kind"] == "slides" for f in r.json()["files"]) == 2)

    section("6. Errors from the pipeline, retry and the daily limit (no AI needed: the recording is not a real video)")
    C = make_user(api, "checkC")
    sidc = new_session(api, C["token"])
    garbage_upload(api, C["token"], sidc)
    r = api("POST", f"/sessions/{sidc}/process", C["token"])
    job, _ = wait_job(api, C["token"], r.json()["job_id"], 120)
    want = ("external_tool", "ffmpeg could not read this file") if key_present else ("llm", "The AI service is not configured, try again later")
    check(f"processing a broken recording fails with a readable message ({want[0]})", job["state"] == "failed" and (job["error_code"], job["error_message"]) == want, f"{job['state']} {job.get('error_code')} {job.get('error_message')}")
    check("the lecture is 'failed' and shows the latest job's error", (lambda s: s["state"] == "failed" and s["latest_job"]["error_code"] == want[0])(api("GET", f"/sessions/{sidc}", C["token"]).json()))
    check("no provider detail or secret in the job", not re.search(r"sk-ant|Traceback|/Users/|/app/", json.dumps(job)))
    r = api("POST", f"/sessions/{sidc}/retry", C["token"])
    check("retry a failed lecture -> 200 and a new job", r.status_code == 200 and r.json()["job_id"] != job["id"])
    wait_job(api, C["token"], r.json()["job_id"], 120)
    check("retry a lecture that is not failed -> 409", api("POST", f"/sessions/{sid}/retry", tokenA).status_code == 409)
    check("notes, quiz and export are 404 until the lecture is done", all(api("GET", f"/sessions/{sidc}/{p}", C["token"]).status_code == 404 for p in ("notes", "quiz", "quiz/attempts", "export.md")))
    # every attempt (also a failed one) counts toward the daily limit
    accepted, limit_msg = 2, None
    for _ in range(40):
        r = api("POST", f"/sessions/{sidc}/process", C["token"])
        if r.status_code == 429:
            limit_msg = err(r)
            break
        accepted += 1
        wait_job(api, C["token"], r.json()["job_id"], 120)
    m = re.search(r"\((\d+) lectures\)", limit_msg[1]) if limit_msg else None
    check("the daily limit stops the user with 429 daily_limit and a clear message", bool(limit_msg) and limit_msg[0] == "daily_limit" and bool(m) and int(m.group(1)) == accepted, f"accepted {accepted}, got {limit_msg}")
    print(f"        (limit reached after {accepted} lectures; message: {limit_msg[1] if limit_msg else None})")
    sidb = new_session(api, B["token"], "B's own lecture")
    garbage_upload(api, B["token"], sidb)
    r = api("POST", f"/sessions/{sidb}/process", B["token"])
    check("the limit is per user: user B can still process a lecture", r.status_code == 200, r.text[:100])
    wait_job(api, B["token"], r.json()["job_id"], 120)

    section("7. Live chunks: the contract and the error cases (no AI needed)")
    D = make_user(api, "checkD")
    sidd = new_session(api, D["token"], "Live check")
    r = api("POST", f"/sessions/{sidd}/live", D["token"])
    check("open a live recording -> chunk_seconds 300, overlap 5", r.status_code == 200 and r.json() == {"chunk_seconds": 300, "overlap": 5}, r.text[:100])
    check("opening again changes nothing", api("POST", f"/sessions/{sidd}/live", D["token"]).json() == {"chunk_seconds": 300, "overlap": 5})
    r = api("POST", f"/sessions/{sidd}/live/chunks", D["token"], params={"index": 0}, files={"chunk": ("notes.txt", b"x", "text/plain")})
    check("a chunk of the wrong type -> 422", r.status_code == 422)
    check("a negative chunk index -> 422", api("POST", f"/sessions/{sidd}/live/chunks", D["token"], params={"index": -1}, files={"chunk": ("c.wav", b"x", "audio/wav")}).status_code == 422)
    replies = [api("POST", f"/sessions/{sidd}/live/chunks", D["token"], params={"index": i}, files={"chunk": ("c.wav", b"not audio", "audio/wav")}) for i in (2, 0)]
    check("chunks are accepted by index, out of order allowed -> {queued: n}", all(r.status_code == 200 for r in replies) and [r.json()["queued"] for r in replies] == [2, 0], str([r.text[:60] for r in replies]))
    jobs = [wait_job(api, D["token"], r.json()["job_id"], 120)[0] for r in replies]
    check("a chunk that is not real audio fails its job with a readable message (and a chunk job never blocks the queue)", all(j["state"] == "failed" and j["error_code"] == "external_tool" for j in jobs), str([(j["state"], j.get("error_code")) for j in jobs]))
    st = api("GET", f"/sessions/{sidd}/live", D["token"], params={"total": 4}).json()
    check("status lists received, failed and missing chunks (a failed chunk counts as missing until sent again)", st["received"] == [0, 2] and st["failed"] == [0, 2] and st["missing"] == [0, 1, 2, 3], json.dumps(st))
    r = api("POST", f"/sessions/{sidd}/live/finish", D["token"], params={"total": 4})
    check("finish with chunks missing -> 422 with a readable message", r.status_code == 422 and "missing" in err(r)[1] and "send" in err(r)[1], r.text[:120])
    print(f"        ({err(r)[1]})")
    check("a lecture opened live cannot also be uploaded or processed as a file", api("POST", f"/sessions/{sidd}/upload", D["token"], files=[("recording", ("a.mp4", b"x", "video/mp4"))]).status_code == 409 and api("POST", f"/sessions/{sidd}/process", D["token"]).status_code == 409)
    check("a lecture that is not live has no live endpoints (409)", api("GET", f"/sessions/{sid}/live", tokenA).status_code == 409)

    # ---- the real pipeline ----------------------------------------------------------------------
    if a.recording and key_present:
        section(f"8. Real pipeline: whole file ({a.clip_seconds} s of the recording, with slides)")
        tmp = Path(tempfile.mkdtemp(prefix="api-check-"))
        clip = tmp / "clip.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-t", str(a.clip_seconds), "-i", a.recording, "-c", "copy", str(clip)], check=True)
        E = make_user(api, "checkE")
        sid_e = new_session(api, E["token"], "Real lecture")
        up = [("recording", (clip.name, open(clip, "rb"), "video/mp4"))] + [("slides", (Path(p).name, open(p, "rb"), "application/pdf")) for p in a.slides]
        r = api("POST", f"/sessions/{sid_e}/upload", E["token"], files=up)
        check("upload the real clip and slides", r.status_code == 200 and r.json()["state"] == "uploaded", r.text[:100])
        t0 = time.time()
        r = api("POST", f"/sessions/{sid_e}/process", E["token"])
        check("start processing -> job id; the lecture is queued or processing", r.status_code == 200 and api("GET", f"/sessions/{sid_e}", E["token"]).json()["state"] in ("queued", "processing"))
        check("starting it again meanwhile -> 409 busy", api("POST", f"/sessions/{sid_e}/process", E["token"]).status_code == 409)
        job, stages = wait_job(api, E["token"], r.json()["job_id"], show=True)
        print(f"        ({time.time() - t0:.0f} s for {a.clip_seconds} s of lecture)")
        check("the job finishes 'done' (100%) and passed through several stages", job["state"] == "done" and job["percent"] == 100 and len(stages) >= 3, f"{job['state']} {job.get('error_message')} stages {stages}")
        s = api("GET", f"/sessions/{sid_e}", E["token"]).json()
        check("lecture state done, config C (slides given), has_notes", s["state"] == "done" and s["config"] == "C" and s["has_notes"], json.dumps({k: s[k] for k in ('state', 'config', 'has_notes')}))
        n = api("GET", f"/sessions/{sid_e}/notes", E["token"]).json()
        check("notes: a title, sections with headings and key points, short notes, the final model", bool(n["title"]) and len(n["full_notes"]) >= 2 and all(x["heading"] and x["key_points"] for x in n["full_notes"]) and len(n["short_notes"]) >= 3 and n["model"] == "claude-sonnet-5-5", json.dumps(n)[:160])
        qr = api("GET", f"/sessions/{sid_e}/quiz", E["token"])
        qs = qr.json()["questions"]
        check("quiz: 10 questions, 4 options each", len(qs) == 10 and all(len(q["options"]) == 4 for q in qs), f"{len(qs)}")
        hidden = all(w not in t for t in (qr.text, api("GET", f"/sessions/{sid_e}", E["token"]).text, api("GET", f"/sessions/{sid_e}/notes", E["token"]).text, api("GET", f"/sessions/{sid_e}/quiz/attempts", E["token"]).text) for w in ("answer_index", "explanation", "correct_index"))
        check("no answer or explanation in any GET before an attempt", hidden)
        check("an attempt with the wrong number of answers -> 422, and nothing is stored", api("POST", f"/sessions/{sid_e}/quiz/attempts", E["token"], json={"answers": [0, 1]}).status_code == 422 and api("GET", f"/sessions/{sid_e}/quiz/attempts", E["token"]).json() == [])
        check("an out-of-range answer -> 422", api("POST", f"/sessions/{sid_e}/quiz/attempts", E["token"], json={"answers": [9] + [0] * 9}).status_code == 422)
        r = api("POST", f"/sessions/{sid_e}/quiz/attempts", E["token"], json={"answers": [0] * 9 + [None]})
        res = r.json()
        right = sum(x["correct"] for x in res["results"])
        check("submit an attempt -> 201, the score equals the number of correct answers, explanations returned", r.status_code == 201 and res["total"] == 10 and res["score"] == right and all(x["explanation"] for x in res["results"]) and res["results"][9]["correct"] is False and res["results"][9]["chosen_index"] is None, r.text[:160])
        print(f"        (answered A to nine questions, skipped one: score {res['score']}/10)")
        api("POST", f"/sessions/{sid_e}/quiz/attempts", E["token"], json={"answers": [res["results"][i]["correct_index"] for i in range(10)]})
        hist = api("GET", f"/sessions/{sid_e}/quiz/attempts", E["token"])
        check("attempt history keeps both, with score and time only", [x["score"] for x in hist.json()] == [res["score"], 10] and "explanation" not in hist.text and "correct" not in hist.text, hist.text[:120])
        r = api("GET", f"/sessions/{sid_e}/export.md", E["token"])
        check("export.md downloads the notes as Markdown", r.status_code == 200 and r.headers["content-type"].startswith("text/markdown") and r.text.startswith("#"), r.text[:60])
        check("the other user cannot read these notes or this job", api("GET", f"/sessions/{sid_e}/notes", B["token"]).status_code == 404 and api("GET", f"/jobs/{job['id']}", B["token"]).status_code == 404)

        section("9. Real pipeline: live chunks (2 chunks of the same clip, slides sent at finish)")
        F = make_user(api, "checkF")
        sid_f = new_session(api, F["token"], "Real live lecture")
        api("POST", f"/sessions/{sid_f}/live", F["token"])
        pieces = [(0, 0, 300), (1, 295, a.clip_seconds)]            # chunk k covers [k*300 - 5, (k+1)*300] (the contract)
        for k, start, end in pieces:
            out = tmp / f"chunk{k}.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(start), "-t", str(end - start), "-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", str(out)], check=True)
            r = api("POST", f"/sessions/{sid_f}/live/chunks", F["token"], params={"index": k}, files={"chunk": (out.name, open(out, "rb"), "audio/wav")})
            check(f"upload chunk {k} -> queued", r.status_code == 200 and r.json()["queued"] == k, r.text[:100])
        r = api("POST", f"/sessions/{sid_f}/live/finish", F["token"], params={"total": 3})
        check("finish claiming 3 chunks when 2 were sent -> 422 'chunk 2 is missing'", r.status_code == 422 and "chunk 2 is missing" in err(r)[1], r.text[:100])
        t0 = time.time()
        r = api("POST", f"/sessions/{sid_f}/live/finish", F["token"], params={"total": 2}, files=[("slides", (Path(p).name, open(p, "rb"), "application/pdf")) for p in a.slides])
        check("finish with the right total and the slides -> job id", r.status_code == 200 and "job_id" in r.json(), r.text[:100])
        job, stages = wait_job(api, F["token"], r.json()["job_id"], show=True)
        print(f"        ({time.time() - t0:.0f} s from finish to done; the chunks were sent back to back, so Whisper ran after 'Stop' here)")
        check("the live lecture ends 'done' with notes and a quiz", job["state"] == "done" and api("GET", f"/sessions/{sid_f}", F["token"]).json()["has_notes"] and len(api("GET", f"/sessions/{sid_f}/quiz", F["token"]).json()["questions"]) == 10, f"{job['state']} {job.get('error_message')}")
        st = api("GET", f"/sessions/{sid_f}/live", F["token"]).json()
        check("chunk status afterwards: both transcribed, none missing", st["transcribed"] == [0, 1] and st["missing"] == [], json.dumps(st))
        check("nothing more can be sent to a finished live lecture (409)", api("POST", f"/sessions/{sid_f}/live/chunks", F["token"], params={"index": 5}, files={"chunk": ("c.wav", b"x", "audio/wav")}).status_code == 409)
        users_extra = [E, F]
    else:
        users_extra = []
        why = "no --recording given" if not a.recording else "the server has no Anthropic API key"
        for name in ("whole-file processing, notes, quiz, attempts, export", "live chunks with Whisper and Claude"):
            skip(name, why)

    section("10. Deleting things")
    r = api("DELETE", f"/sessions/{sid}", tokenA)
    check("delete a lecture -> 204, then 404", r.status_code == 204 and api("GET", f"/sessions/{sid}", tokenA).status_code == 404)
    check("delete a module and a course -> 204", api("DELETE", f"/modules/{mid}", tokenA).status_code == 204 and api("DELETE", f"/courses/{cid}", tokenA).status_code == 204 and api("GET", "/courses", tokenA).json() == [])
    if not a.keep:
        deleted = [api("DELETE", "/auth/me", t).status_code for t in [tokenA] + [u["token"] for u in [B, C, D] + users_extra]]
        check("delete the accounts -> 204 (all their data goes)", all(d == 204 for d in deleted), str(deleted))
        check("a deleted account's token and login stop working", api("GET", "/auth/me", tokenA).status_code == 401 and api("POST", "/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401)

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n{sum(r[0] == 'PASS' for r in RESULTS)} passed, {len(fails)} failed, {sum(r[0] == 'SKIP' for r in RESULTS)} skipped")
    for _, name, detail in fails:
        print(f"  FAILED: {name}  ({detail})")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--verify-cmd", required=True, help="shell command that confirms an address, {email} is replaced (see the top of this file)")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed certificate (Caddy on localhost)")
    ap.add_argument("--recording", help="a real recording: enables the checks that call Whisper and Claude")
    ap.add_argument("--slides", nargs="*", default=[], help="slide files for the real checks")
    ap.add_argument("--clip-seconds", type=int, default=330, help="how much of the recording to use (needs more than 300 for the live check)")
    ap.add_argument("--keep", action="store_true", help="do not delete the test users at the end")
    args = ap.parse_args()
    VERIFY_CMD = args.verify_cmd
    sys.exit(run(args))
