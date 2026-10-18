# lecturekit-server

FastAPI backend for lecturekit (accounts, courses, uploads, background processing, notes, quiz). It is built from
`../SERVER_PLAN.md` and imports only `lecturekit.api` (plus `lecturekit.errors` and the constants
`lecturekit.chunks.CHUNK_SECONDS/OVERLAP` and `lecturekit.slidefiles.SLIDE_TYPES`). It never edits the package.

## Layout

```
lecturekit-server/      this folder (the server project, its own git repository)
  server/               the Python package (uvicorn server.main:app)
    main.py settings.py db.py models.py schemas.py security.py errors.py access.py storage.py
    jobs.py serialize.py worker.py admin.py
    routes/             auth.py courses.py uploads.py live.py results.py health.py
  tests/                offline tests (fake Whisper and Claude)
  scripts/smoke.py      drives a running server over HTTP (manual acceptance)
  scripts/api_check.py  checks every feature over HTTP (72 checks, prints PASS/FAIL)
  scripts/make_postman.py + postman/   the Postman collection (126 requests with tests) and its sample files
  requirements.txt      server dependencies (lecturekit has its own, ../lecturekit/requirements.txt)
../lecturekit/          the pipeline package (unchanged)
../docker-compose.yml   starts everything in Docker (with ../docker/, ../Caddyfile, ../.env.example, ../DEPLOY.md)
```

All routes live under `/api` (the web app is served at `/`). `GET /health` is also at the root.
The paths in `SERVER_PLAN.md` section 7 are relative to `/api`.

## Run it locally

```bash
cd lecturekit-server
python3 -m venv .venv && source .venv/bin/activate
pip install -r ../lecturekit/requirements.txt -r requirements.txt
python -m pytest -q                                   # offline, about 6 seconds

# a real run (Whisper + Claude). Start it from the lecturekit folder so its .env (the API key) is found:
cd ../lecturekit
DATA_DIR=../lecturekit-server/dev_data JWT_SECRET=$(openssl rand -hex 32) PYTHONPATH=. \
  ../lecturekit-server/.venv/bin/uvicorn server.main:app --app-dir ../lecturekit-server --port 8000 --workers 1
```

Then, in another terminal:

```bash
cd lecturekit-server
.venv/bin/python scripts/smoke.py --recording talk.mp4 --slides slides.pdf          # whole file
PYTHONPATH=../lecturekit .venv/bin/python scripts/smoke.py --recording talk.mp4 --live --end 600
```

Interactive API docs: `http://localhost:8000/api/docs`.

## Settings (environment variables)

Logs: one file per day in `DATA_DIR/logs`, `server-YYYY-MM-DD.log` (every request line, job events, warnings, errors with tracebacks) and `error-YYYY-MM-DD.log` (errors only); files older than `LOG_RETENTION_DAYS` (default 14) are deleted. Times are UTC in Docker. Passwords, tokens and request bodies are never logged.

See `../.env.example`. `JWT_SECRET` (32+ characters) is required: the server refuses to start without it. Sign-up sends a verification code by email (HTML, see `server/mail_templates.py`) and a welcome email after it is confirmed (`SMTP_*`, `PUBLIC_URL`; without `SMTP_HOST` the email goes to the log) and Google login is on when `GOOGLE_CLIENT_ID` is set.
`DATA_DIR` holds the database, uploads, results, cache and logs.

## Rules the code follows

- One worker thread and one FIFO queue: one heavy job at a time, `--workers 1` only.
- The server always uses the final model (`LK_MODEL_FINAL`, Sonnet 5.5), never the cheap dev model.
- Quiz answers and explanations leave the server only in the reply to a submitted attempt.
- Every query goes through the logged-in user; someone else's id answers 404, like an unknown id.
- Passwords: bcrypt. Tokens: JWT HS256, 7 days, `Authorization: Bearer` only (no cookies).
- Errors: `{"error": {"code": "...", "message": "..."}}`; provider details and secrets never reach a client.

## Owner commands

```bash
python -m server.admin list-users
python -m server.admin set-password someone@example.com        # asks for the new password
```
(Inside the container, from the parent folder: `docker compose exec app python -m server.admin ...`.)
