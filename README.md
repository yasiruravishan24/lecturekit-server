# lecturekit server

FastAPI backend for lecturekit (accounts, courses, uploads, background processing, notes, quiz). It is built from
`../SERVER_PLAN.md` and imports only `lecturekit.api` (plus `lecturekit.errors` and the constants
`lecturekit.chunks.CHUNK_SECONDS/OVERLAP` and `lecturekit.slidefiles.SLIDE_TYPES`). It never edits the package.

## Layout

```
server/                 this folder (the server project)
  server/               the Python package (uvicorn server.main:app)
    main.py settings.py db.py models.py schemas.py security.py errors.py access.py storage.py
    jobs.py serialize.py worker.py admin.py
    routes/             auth.py courses.py uploads.py live.py results.py health.py
  tests/                offline tests (fake Whisper and Claude)
  scripts/smoke.py      drives a running server over HTTP (manual acceptance)
  deploy/               Dockerfile, docker-compose.yml, Caddyfile, .env.example, DEPLOY.md
  requirements.txt      server dependencies (lecturekit has its own, ../lecturekit/requirements.txt)
../lecturekit/          the pipeline package (unchanged)
```

All routes live under `/api` (the web app is served at `/`). `GET /health` is also at the root.
The paths in `SERVER_PLAN.md` section 7 are relative to `/api`.

## Run it locally

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r ../lecturekit/requirements.txt -r requirements.txt
python -m pytest -q                                   # offline, about 6 seconds

# a real run (Whisper + Claude). Start it from the lecturekit folder so its .env (the API key) is found:
cd ../lecturekit
DATA_DIR=../server/dev_data JWT_SECRET=$(openssl rand -hex 32) INVITE_CODE=dev-invite PYTHONPATH=. \
  ../server/.venv/bin/uvicorn server.main:app --app-dir ../server --port 8000 --workers 1
```

Then, in another terminal:

```bash
cd server
.venv/bin/python scripts/smoke.py --invite dev-invite --recording talk.mp4 --slides slides.pdf          # whole file
PYTHONPATH=../lecturekit .venv/bin/python scripts/smoke.py --invite dev-invite --recording talk.mp4 --live --end 600
```

Interactive API docs: `http://localhost:8000/docs`.

## Settings (environment variables)

See `deploy/.env.example`. `JWT_SECRET` (32+ characters) and `INVITE_CODE` are required: the server refuses to start without them.
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
(Inside the container: `docker compose exec app python -m server.admin ...`.)
