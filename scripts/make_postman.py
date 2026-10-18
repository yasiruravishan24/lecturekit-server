"""Writes the Postman collection and environment:  python scripts/make_postman.py

  postman/lecturekit.postman_collection.json         one request per route, grouped by resource
  postman/lecturekit.local.postman_environment.json  baseUrl, password, fullName, otpCode

Edit this script and run it again; do not edit the JSON by hand.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "postman"

# helpers available in every script of the collection
LIB = r"""
const body = () => { try { return pm.response.json(); } catch (e) { return {}; } };
const set = (k, v) => pm.collectionVariables.set(k, v);
const get = (k) => pm.collectionVariables.get(k);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const call = (method, path, opts = {}) => new Promise((resolve, reject) => {
  const header = {};
  if (!opts.anonymous) header.Authorization = "Bearer " + (opts.token || get("token"));
  if (opts.json !== undefined) header["Content-Type"] = "application/json";
  if (opts.contentType) header["Content-Type"] = opts.contentType;
  const req = { url: pm.variables.replaceIn("{{baseUrl}}/api" + path), method, header };
  if (opts.json !== undefined) req.body = { mode: "raw", raw: JSON.stringify(opts.json) };
  if (opts.raw !== undefined) req.body = { mode: "raw", raw: opts.raw };
  pm.sendRequest(req, (err, res) => {
    if (err) return reject(err);
    let json = {}; try { json = res.json(); } catch (e) { /* no body */ }
    resolve({ code: res.code, json });
  });
});
const waitForJob = async (jobId, token) => {
  for (let i = 0; i < 400; i++) {
    const r = await call("GET", "/jobs/" + jobId, { token });
    if (r.json.state === "done" || r.json.state === "failed") return r.json;
    await sleep(2000);
  }
  return { state: "timeout" };
};
"""


def status(code, *more):
    if more:
        codes = [code, *more]
        return f'pm.test("Status is {" or ".join(map(str, codes))}", () => pm.expect({codes}).to.include(pm.response.code));'
    return f'pm.test("Status is {code}", () => pm.response.to.have.status({code}));'


def script(kind, text):
    return {"listen": kind, "script": {"type": "text/javascript", "exec": (LIB + text).strip("\n").split("\n")}}


def doc(summary, auth="Bearer token", params="", body="", responses=()):
    out = [summary, "", f"**Authentication:** {auth}"]
    if params:
        out += ["", "**Parameters:** " + params]
    if body:
        out += ["", "**Body:** " + body]
    out += ["", "**Responses**", *[f"- `{code}` {text}" for code, text in responses]]
    return "\n".join(out)


def url(path, query=None):
    node = {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": [p for p in path.split("/") if p]}
    if query:
        node["query"] = [{"key": k, "value": v, "description": d} for k, v, d in query]
        node["raw"] += "?" + "&".join(f"{k}={v}" for k, v, _ in query)
    return node


def request(name, method, path, description, tests="", *, json_body=None, form=None, query=None, auth="inherit", pre=""):
    """auth: 'inherit' (collection bearer token), 'none', or the name of a variable holding another token"""
    req = {"method": method, "header": [], "url": url(path, query), "description": description}
    if auth == "none":
        req["auth"] = {"type": "noauth"}
    elif auth != "inherit":
        req["auth"] = {"type": "bearer", "bearer": [{"key": "token", "value": "{{%s}}" % auth, "type": "string"}]}
    if json_body is not None:
        req["header"].append({"key": "Content-Type", "value": "application/json"})
        req["body"] = {"mode": "raw", "raw": json.dumps(json_body, indent=2), "options": {"raw": {"language": "json"}}}
    if form is not None:
        req["body"] = {"mode": "formdata", "formdata": [{"key": k, "type": "file", "src": src, "description": d} for k, src, d in form]}
    item = {"name": name, "request": req, "event": []}
    if pre:
        item["event"].append(script("prerequest", pre))
    if tests:
        item["event"].append(script("test", tests))
    return item


def group(name, items, description=""):
    return {"name": name, "description": description, "item": items}


RECORDING = "mp4, mov, m4a, mp3, wav, webm, mkv, aac, ogg"
SLIDES = "pdf, pptx, ppt, pps, ppsx, odp, docx, doc, odt, rtf"
ERR = '{"error": {"code": "...", "message": "..."}}'

WAIT_FOR_JOB = r"""
set("jobId", body().job_id);
const started = body();
(async () => {
  const job = await waitForJob(started.job_id);
  pm.test("The job finishes with state done", () => pm.expect(job.state, job.error_message).to.eql("done"));
})();"""


def build():
    system = group("System", [
        request("Health Check", "GET", "/health", doc("Reports whether the server and its tools are ready. Never returns a secret.", "none",
                responses=[(200, "`{ok, checks, optional_missing, models, whisper, queue_length}`; `ok` is false while a required tool or the Anthropic API key is missing")]),
                status(200) + '\npm.test("Has ok, checks and queue_length", () => pm.expect(body()).to.include.all.keys("ok", "checks", "queue_length"));'
                '\npm.test("Contains no secret", () => pm.expect(pm.response.text()).to.not.match(/JWT|sk-ant|data_dir/i));', auth="none"),
        request("API Health Check", "GET", "/api/health", doc("The same health check under the `/api` prefix.", "none", responses=[(200, "same body as Health Check")]),
                status(200), auth="none"),
        request("Server Info", "GET", "/", doc("What this server is. When the web app is installed, `/` serves the web app instead.", "none",
                responses=[(200, "`{name, api, docs, health, web_app}`")]), status(200), auth="none"),
        request("OpenAPI Schema", "GET", "/api/openapi.json", doc("The machine-readable description of every route.", "none", responses=[(200, "OpenAPI 3 document")]),
                status(200) + '\npm.test("Lists the routes", () => pm.expect(Object.keys(body().paths).length).to.be.above(20));', auth="none"),
        request("API Docs", "GET", "/api/docs", doc("Interactive API documentation (Swagger UI) for the browser.", "none", responses=[(200, "HTML page")]),
                status(200), auth="none"),
    ])

    credentials = {"email": "{{userEmail}}", "password": "{{password}}"}
    auth = group("Auth", [
        request("Get Auth Config", "GET", "/api/auth/config", doc("What a client needs before login: whether Google sign-in is switched on, and the Google client ID to use.", "none",
                responses=[(200, "`{google_enabled, google_client_id}`")]),
                status(200) + '\npm.test("Has google_enabled and google_client_id", () => pm.expect(body()).to.have.all.keys("google_enabled", "google_client_id"));', auth="none"),
        request("Register", "POST", "/api/auth/register", doc(
            "Creates an account and emails a 6-digit code (valid 10 minutes). The account cannot log in until the code is confirmed with Verify Email. The email is stored in lower case. "
            "Registering again with an address that is still unconfirmed replaces the name and password and sends a new email. "
            "Unless the environment variable `email` is set, a new unique email is made on every run. The code is only in the email, never in the reply.", "none",
            body="`full_name` (1 to 120 characters), `email` (valid address), `password` (at least 8 characters), `accept_terms` (must be true)",
            responses=[(201, "`{user: {id, email, full_name, email_verified, google_linked, created_at}, verification_sent}`"), (409, "`email_taken`: a confirmed account exists"), (422, "`validation`: bad email, short password, empty name or terms not accepted")]),
            status(201, 409) + '\npm.test("Never returns the password or its hash", () => pm.expect(pm.response.text()).to.not.match(/password|hash/i));\npm.test("The reply does not contain the email code", () => pm.expect(pm.response.text()).to.not.match(/verification_code|\\b\\d{6}\\b/));',
            json_body={"full_name": "{{fullName}}", **credentials, "accept_terms": True}, auth="none",
            pre='const email = pm.environment.get("email") || ("user" + Date.now() + "@example.com");\nset("userEmail", email);'),
        request("Verify Email", "POST", "/api/auth/verify-email", doc(
            "Confirms the email address with the 6-digit code from the email, then sends a welcome email. Five wrong codes cancel the code (ask for a new one with Resend Verification Email). "
            "Put the code from the email into the environment variable `otpCode` first.", "none",
            body="`email`, `code`",
            responses=[(200, "`{user}` with `email_verified: true`"), (400, "`invalid_code` (wrong) or `code_expired`"), (429, "`too_many_attempts`: five wrong codes, ask for a new one")]),
            'if (!pm.environment.get("otpCode")) { pm.test.skip("Set the environment variable otpCode to the code from the email"); } else {\n'
            + status(200) + '\npm.test("Email is verified", () => pm.expect(body().user.email_verified).to.eql(true));\n}',
            json_body={"email": "{{userEmail}}", "code": "{{otpCode}}"}, auth="none"),
        request("Resend Verification Email", "POST", "/api/auth/resend-verification", doc(
            "Sends a new code by email (at most once a minute per address); the old code stops working. The answer is always the same, so it never shows whether an account exists.", "none",
            body="`email`", responses=[(200, "`{sent: true}`")]),
            status(200) + '\npm.test("Always answers sent", () => pm.expect(body()).to.eql({ sent: true }));',
            json_body={"email": "{{userEmail}}"}, auth="none"),
        request("Login", "POST", "/api/auth/login", doc(
            "Returns a token valid for 7 days. It is sent as `Authorization: Bearer <token>`; this collection saves it and does that for you. "
            "The email must be confirmed first. A wrong password and an unknown email give the same `401`. After 5 failed logins for one email the account is locked for 10 minutes.", "none",
            body="`email`, `password`",
            responses=[(200, "`{token, expires_at, user}`"), (401, "`bad_credentials`"), (403, "`email_not_verified`"), (429, "`too_many_attempts`")]),
            status(200) + '\npm.test("Returns token, expires_at and user", () => pm.expect(body()).to.include.all.keys("token", "expires_at", "user"));\nset("token", body().token);',
            json_body=credentials, auth="none"),
        request("Login With Google", "POST", "/api/auth/google", doc(
            "Logs in, or signs up the first time, with a Google ID token that the web app or extension gets from Google sign-in. A Google account counts as a confirmed email, "
            "and an existing account with the same email is linked. Needs `GOOGLE_CLIENT_ID` on the server. "
            "This collection sends a fake token, so it checks that a bad token is refused; test the real flow from the web app.", "none",
            body="`id_token`, `accept_terms` (true the first time, when the account is created)",
            responses=[(200, "`{token, expires_at, user}`"), (401, "`google_invalid`: the token is not valid for this app"), (422, "`terms_required`"), (503, "`google_disabled`: not set up on this server")]),
            'pm.test("A fake token is refused", () => pm.expect([401, 503]).to.include(pm.response.code));\npm.test("Uses the standard error shape", () => pm.expect(body().error.code).to.be.oneOf(["google_invalid", "google_disabled"]));',
            json_body={"id_token": "not-a-real-google-token", "accept_terms": True}, auth="none"),
        request("Get Current User", "GET", "/api/auth/me", doc("The logged-in user.", responses=[(200, "`{user: {id, email, full_name, email_verified, google_linked, created_at}}`"), (401, "missing, expired or invalid token")]),
                status(200) + '\npm.test("Is the user from Login", () => pm.expect(body().user.email).to.eql(get("userEmail")));'),
    ], "Accounts and login.")

    courses = group("Courses", [
        request("List Courses", "GET", "/api/courses", doc("The logged-in user's courses.", responses=[(200, "`[{id, name, created_at}]`")]),
                status(200) + '\npm.test("Is a list", () => pm.expect(body()).to.be.an("array"));'),
        request("Create Course", "POST", "/api/courses", doc("Creates a course.", body="`name` (1 to 120 characters, trimmed)", responses=[(201, "the course `{id, name, created_at}`"), (422, "`validation`")]),
                status(201) + '\nset("courseId", body().id);', json_body={"name": "Introduction to Programming"}),
        request("Update Course", "PATCH", "/api/courses/{{courseId}}", doc("Renames a course.", params="`course_id` in the path", body="`name`",
                responses=[(200, "the course"), (404, "not found, or not yours"), (422, "`validation`")]),
                status(200) + '\npm.test("Is renamed", () => pm.expect(body().name).to.eql("Introduction to Programming 101"));', json_body={"name": "Introduction to Programming 101"}),
        request("Delete Course", "DELETE", "/api/courses/{{scratchCourseId}}", doc(
            "Deletes a course with its modules, lectures and files. Refused while one of its lectures is being processed. This collection deletes a throw-away course it creates first.",
            params="`course_id` in the path", responses=[(204, "deleted"), (404, "not found, or not yours"), (409, "`busy`")]),
            status(204), pre='(async () => { set("scratchCourseId", (await call("POST", "/courses", { json: { name: "Scratch course" } })).json.id); })();'),
    ])

    modules = group("Modules", [
        request("List Modules", "GET", "/api/courses/{{courseId}}/modules", doc("The modules of a course.", params="`course_id` in the path",
                responses=[(200, "`[{id, course_id, name, created_at}]`"), (404, "not found")]),
                status(200) + '\npm.test("Is a list", () => pm.expect(body()).to.be.an("array"));'),
        request("Create Module", "POST", "/api/courses/{{courseId}}/modules", doc("Creates a module (for example a week) in a course.", params="`course_id` in the path", body="`name`",
                responses=[(201, "the module"), (404, "course not found"), (422, "`validation`")]),
                status(201) + '\nset("moduleId", body().id);', json_body={"name": "Week 1"}),
        request("Update Module", "PATCH", "/api/modules/{{moduleId}}", doc("Renames a module.", params="`module_id` in the path", body="`name`", responses=[(200, "the module"), (404, "not found")]),
                status(200) + '\npm.test("Is renamed", () => pm.expect(body().name).to.eql("Week 1: Strings"));', json_body={"name": "Week 1: Strings"}),
        request("Delete Module", "DELETE", "/api/modules/{{scratchModuleId}}", doc(
            "Deletes a module with its lectures and files. Refused while one of its lectures is being processed. This collection deletes a throw-away module it creates first.",
            params="`module_id` in the path", responses=[(204, "deleted"), (404, "not found"), (409, "`busy`")]),
            status(204), pre='(async () => { set("scratchModuleId", (await call("POST", "/courses/" + get("courseId") + "/modules", { json: { name: "Scratch module" } })).json.id); })();'),
    ])

    sessions = group("Sessions", [
        request("List Sessions", "GET", "/api/modules/{{moduleId}}/sessions", doc("The lectures (sessions) of a module with their state.", params="`module_id` in the path",
                responses=[(200, "`[Session]`, see Get Session"), (404, "not found")]), status(200) + '\npm.test("Is a list", () => pm.expect(body()).to.be.an("array"));'),
        request("Create Session", "POST", "/api/modules/{{moduleId}}/sessions", doc("Creates an empty lecture. Upload a recording next, or open a live recording.", params="`module_id` in the path",
                body="`title` (1 to 160 characters)", responses=[(201, "the session, state `empty`"), (404, "module not found"), (422, "`validation`")]),
                status(201) + '\npm.test("Starts empty", () => pm.expect(body().state).to.eql("empty"));\nset("sessionId", body().id);', json_body={"title": "Lecture 2: Strings"}),
        request("Get Session", "GET", "/api/sessions/{{sessionId}}", doc(
            "One lecture. `state` is `empty`, `uploaded`, `live`, `queued`, `processing`, `done` or `failed`. `latest_job` is the most recent job; `has_notes` is true when notes exist.",
            params="`session_id` in the path", responses=[(200, "`{id, module_id, title, state, source, has_slides, config, created_at, files, latest_job, has_notes}`"), (404, "not found, or not yours")]),
            status(200) + '\npm.test("Has a state", () => pm.expect(body().state).to.be.a("string"));'),
        request("Update Session", "PATCH", "/api/sessions/{{sessionId}}", doc("Renames a lecture.", params="`session_id` in the path", body="`title`", responses=[(200, "the session"), (404, "not found"), (422, "`validation`")]),
                status(200) + '\npm.test("Is renamed", () => pm.expect(body().title).to.eql("Lecture 2: Strings and Input"));', json_body={"title": "Lecture 2: Strings and Input"}),
        request("Delete Session", "DELETE", "/api/sessions/{{scratchSessionId}}", doc(
            "Deletes a lecture with its recording, slides, notes and quiz attempts. Refused while it is being processed. This collection deletes a throw-away lecture it creates first.",
            params="`session_id` in the path", responses=[(204, "deleted"), (404, "not found"), (409, "`busy`")]),
            status(204), pre='(async () => { set("scratchSessionId", (await call("POST", "/modules/" + get("moduleId") + "/sessions", { json: { title: "Scratch lecture" } })).json.id); })();'),
    ])

    failed_session = r"""(async () => {
  // a lecture whose processing failed (the file is not a real recording), so that it can be retried
  const created = await call("POST", "/modules/" + get("moduleId") + "/sessions", { json: { title: "Failed lecture" } });
  const id = created.json.id;
  const boundary = "lkBoundary";
  const part = "--" + boundary + "\r\nContent-Disposition: form-data; name=\"recording\"; filename=\"broken.mp4\"\r\nContent-Type: video/mp4\r\n\r\nthis is not a video\r\n--" + boundary + "--\r\n";
  await call("POST", "/sessions/" + id + "/upload", { raw: part, contentType: "multipart/form-data; boundary=" + boundary });
  const started = await call("POST", "/sessions/" + id + "/process");
  await waitForJob(started.json.job_id);
  set("failedSessionId", id);
})();"""
    processing = group("Processing", [
        request("Upload Files", "POST", "/api/sessions/{{sessionId}}/upload", doc(
            "Uploads the lecture recording and optional slides. A new recording replaces the old one; slides are added in the order sent (several files are joined into one). Anything made from earlier files is discarded.",
            params="`session_id` in the path",
            body=f"multipart form: `recording` (one file: {RECORDING}) and `slides` (any number of files: {SLIDES})",
            responses=[(200, "the session, state `uploaded`"), (409, "`busy` (being processed) or `live_session`"), (413, "`too_large`"), (422, "`validation` (type) or `missing_input` (no files)")]),
            status(200) + '\npm.test("State is uploaded", () => pm.expect(body().state).to.eql("uploaded"));\npm.test("Has the recording and the slides", () => pm.expect(body().files.map(f => f.kind)).to.eql(["recording", "slides"]));',
            form=[("recording", "sample-recording.m4a", "audio or video of the lecture"), ("slides", "sample-slides.pdf", "optional, repeat for several files")]),
        request("Process Session", "POST", "/api/sessions/{{sessionId}}/process", doc(
            "Starts turning the recording into notes and a quiz (Whisper, then Claude). It runs in the background, one job at a time; poll Get Job (or Get Session) for progress. In this collection the request also waits until the job has finished.",
            params="`session_id` in the path",
            responses=[(200, "`{job_id}`"), (409, "`busy` (already queued or processing) or `live_session`"), (422, "`missing_input`: no recording uploaded"), (429, "`daily_limit`")]),
            status(200) + WAIT_FOR_JOB),
        request("Retry Session", "POST", "/api/sessions/{{failedSessionId}}/retry", doc(
            "Processes a failed lecture again, reusing the files already uploaded. This collection first prepares a lecture that failed.",
            params="`session_id` in the path", responses=[(200, "`{job_id}`"), (409, "`not_failed` (the lecture has not failed) or `busy`"), (429, "`daily_limit`")]),
            status(200) + '\npm.test("A new job is started", () => pm.expect(body().job_id).to.be.a("number"));', pre=failed_session),
    ])

    jobs = group("Jobs", [
        request("Get Job", "GET", "/api/jobs/{{jobId}}", doc(
            "Status of one background job. `state`: `queued`, `processing`, `done`, `failed`. While processing, `stage` (prepare, transcribe, slides, align, filter, notes, quiz, write, done) and `percent` show progress. When failed, `error_code` and `error_message` explain why in plain words.",
            params="`job_id` in the path",
            responses=[(200, "`{id, session_id, kind, state, stage, percent, message, error_code, error_message, created_at, started_at, finished_at}`"), (404, "not found, or not yours")]),
            status(200) + '\npm.test("Has a state and a percent", () => { pm.expect(body().state).to.be.a("string"); pm.expect(body().percent).to.be.a("number"); });'),
    ])

    results = group("Results", [
        request("Get Notes", "GET", "/api/sessions/{{sessionId}}/notes", doc("The full notes and short notes of a finished lecture.", params="`session_id` in the path",
                responses=[(200, "`{title, full_notes: [{heading, summary, key_points}], short_notes, model, config, created_at}`"), (404, "not found, or the lecture is not done yet")]),
                status(200) + '\npm.test("Has a title, sections and short notes", () => { pm.expect(body().title).to.not.be.empty; pm.expect(body().full_notes.length).to.be.above(0); pm.expect(body().short_notes.length).to.be.above(0); });'),
        request("Get Quiz", "GET", "/api/sessions/{{sessionId}}/quiz", doc("The quiz questions. The correct answers and explanations are not included: they are returned only after an attempt is submitted.", params="`session_id` in the path",
                responses=[(200, "`{questions: [{id, position, question, options, bloom_level}]}`"), (404, "lecture not done")]),
                status(200) + '\nset("questionCount", body().questions.length);\npm.test("Questions have four options", () => body().questions.forEach(q => pm.expect(q.options).to.have.length(4)));\npm.test("Contains no answer or explanation", () => pm.expect(pm.response.text()).to.not.match(/answer_index|explanation/));'),
        request("Submit Quiz Attempt", "POST", "/api/sessions/{{sessionId}}/quiz/attempts", doc(
            "Scores an attempt and returns the correct answers and explanations. Every attempt is kept.", params="`session_id` in the path",
            body="`answers`: one entry per question, in order: the index (0 to 3) of the chosen option, or `null` to skip",
            responses=[(201, "`{attempt_id, score, total, results: [{question_id, position, chosen_index, correct_index, correct, explanation}]}`"), (404, "lecture not done"), (422, "`validation`: wrong number of answers or an option out of range")]),
            status(201) + '\npm.test("Score equals the number of correct answers", () => pm.expect(body().score).to.eql(body().results.filter(r => r.correct).length));\npm.test("Explanations are returned now", () => body().results.forEach(r => pm.expect(r.explanation).to.not.be.empty));',
            json_body={"answers": [0, 1, 2, 3, 0, 1, 2, 3, 0, None]},
            pre='const n = Number(get("questionCount") || 10);\npm.request.body.update({ mode: "raw", raw: JSON.stringify({ answers: Array.from({ length: n }, (_, i) => (i === n - 1 ? null : i % 4)) }) });'),
        request("List Quiz Attempts", "GET", "/api/sessions/{{sessionId}}/quiz/attempts", doc("The history of attempts: score and time only, never the answers.", params="`session_id` in the path",
                responses=[(200, "`[{id, score, total, submitted_at}]`"), (404, "lecture not done")]),
                status(200) + '\npm.test("Contains the attempt just made", () => pm.expect(body().length).to.be.above(0));'),
        request("Export Notes", "GET", "/api/sessions/{{sessionId}}/export.md", doc("The notes as a Markdown file to download.", params="`session_id` in the path",
                responses=[(200, "`text/markdown` attachment `lecture-<id>.md`"), (404, "lecture not done")]),
                status(200) + '\npm.test("Is Markdown", () => { pm.expect(pm.response.headers.get("Content-Type")).to.include("text/markdown"); pm.expect(pm.response.text()).to.match(/^#/); });'),
    ])

    live = group("Live Recording", [
        request("Open Live Session", "POST", "/api/sessions/{{liveSessionId}}/live", doc(
            "Turns an empty lecture into a live recording and returns the chunk contract the client must follow: audio is sent in pieces of `chunk_seconds`, each starting `overlap` seconds early. Repeating the call changes nothing. This collection first creates an empty lecture.",
            params="`session_id` in the path", responses=[(200, "`{chunk_seconds: 300, overlap: 5}`"), (409, "`not_empty` (the lecture already has a recording)")]),
            status(200) + '\npm.test("Returns the chunk contract", () => pm.expect(body()).to.eql({ chunk_seconds: 300, overlap: 5 }));',
            pre='(async () => { set("liveSessionId", (await call("POST", "/modules/" + get("moduleId") + "/sessions", { json: { title: "Live lecture" } })).json.id); })();'),
        request("Upload Chunk", "POST", "/api/sessions/{{liveSessionId}}/live/chunks", doc(
            "Sends one piece of audio. Whisper starts on it at once, while the lecture is still being recorded. Pieces may arrive in any order; the same `index` again redoes that piece.",
            params="`session_id` in the path; query `index` (0, 1, 2 ...)", body=f"multipart form: `chunk` (one audio file: {RECORDING})",
            responses=[(200, "`{queued, job_id}`"), (409, "not a live lecture, or already being processed"), (413, "`too_large`"), (422, "`validation`"), (429, "`chunk_limit` (200 per lecture)")]),
            status(200) + '\npm.test("Is queued", () => pm.expect(body().queued).to.eql(0));',
            query=[("index", "0", "position of this piece, starting at 0")], form=[("chunk", "chunk0.m4a", "the audio of this piece")]),
        request("Get Live Status", "GET", "/api/sessions/{{liveSessionId}}/live", doc(
            "Which pieces arrived, which are transcribed or failed, and which are missing for a given total.", params="`session_id` in the path; query `total` (optional): how many pieces the recording has",
            responses=[(200, "`{chunk_seconds, overlap, total, received, transcribed, failed, missing}`"), (409, "`not_live`")]),
            status(200) + '\npm.test("Piece 0 has arrived", () => pm.expect(body().received).to.include(0));\npm.test("Nothing is missing", () => pm.expect(body().missing).to.eql([]));',
            query=[("total", "1", "number of pieces sent")]),
        request("Finish Live Session", "POST", "/api/sessions/{{liveSessionId}}/live/finish", doc(
            "After Stop: merges the pieces, reads the slides, and makes the notes and quiz in the background. Fails at once, with a readable message, if a piece is missing. In this collection the request also waits until the job has finished.",
            params="`session_id` in the path; query `total` (required): how many pieces were sent",
            body=f"optional multipart form: `slides` (any number of files: {SLIDES})",
            responses=[(200, "`{job_id}`"), (409, "`busy`, or not a live lecture"), (422, "`missing_input`: for example \"chunk 2 is missing, send it and finish again\""), (429, "`daily_limit`")]),
            status(200) + WAIT_FOR_JOB,
            query=[("total", "1", "number of pieces sent")], form=[("slides", "sample-slides.pdf", "optional")]),
    ], "Used by the browser extension: the lecture is sent in pieces while it is recorded.")

    account = group("Account", [
        request("Delete Account", "DELETE", "/api/auth/me", doc(
            "Permanently deletes the account and everything it owns: courses, lectures, recordings, notes and quiz attempts. Refused while one of the account's lectures is being processed. "
            "This is the account you are logged in with, so it is the last request of the collection. The next run starts with Register and a new code.",
            responses=[(204, "deleted"), (401, "invalid token"), (409, "`busy`: a lecture is still being processed")]),
            status(204)),
    ])

    return [system, auth, courses, modules, sessions, processing, jobs, results, live, account]


def main():
    OUT.mkdir(exist_ok=True)
    folders = build()
    collection = {
        "info": {
            "name": "LectureKit API",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": "REST API of the LectureKit server: accounts, courses, lectures, upload, processing, notes, quiz and live recording.\n\n"
                           "Setup: import this collection and the environment, set the Postman working directory to `postman/sample-files`, "
                           "then run Register, copy the 6-digit code from the email into `otpCode`, and run Verify Email and Login; the token is saved and sent automatically. Error replies look like `" + ERR + "`.",
        },
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}]},
        "variable": [{"key": k, "value": ""} for k in ("token", "userEmail", "courseId", "moduleId", "sessionId", "jobId", "questionCount")],
        "item": folders,
    }
    environment = {
        "name": "LectureKit Local",
        "values": [
            {"key": "baseUrl", "value": "https://localhost:8443", "type": "default", "enabled": True},
            {"key": "password", "value": "Passw0rd-for-tests", "type": "default", "enabled": True},
            {"key": "fullName", "value": "Postman Tester", "type": "default", "enabled": True},
            {"key": "otpCode", "value": "", "type": "default", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }
    (OUT / "lecturekit.postman_collection.json").write_text(json.dumps(collection, indent=2) + "\n")
    (OUT / "lecturekit.local.postman_environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    print(f"wrote the collection: {len(folders)} folders, {sum(len(f['item']) for f in folders)} requests")


if __name__ == "__main__":
    main()
