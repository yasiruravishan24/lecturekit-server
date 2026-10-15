# DEPLOY.md: putting the server on the internet (written for a beginner)

Result: `https://<your-domain>/health` answers, and you can register, upload a lecture and read the notes.
You need: a small Linux server, a domain name, the Claude API key, about an hour (most of it waiting for the first build).

## 0. What you need to decide

| Thing | Suggestion |
|---|---|
| Server | Ubuntu 22.04 or 24.04, **4 vCPU, 8 GB RAM, 40 GB disk** (minimum 2 vCPU, 4 GB, 40 GB). Any provider: Hetzner, DigitalOcean, AWS Lightsail, ... |
| Domain | a free DuckDNS name (https://www.duckdns.org) pointing at the server's IP address |
| Invite code | any phrase; only people who know it can register |

## 1. Create the server and point the domain at it

1. Create the Ubuntu server and note its **public IP address**. Log in: `ssh <user>@<ip>`.
2. DuckDNS: sign in, create a name (for example `mylectures`), set its IP to the server's IP. Your domain is `mylectures.duckdns.org`.
3. Open the firewall for web traffic (and keep SSH):
   ```bash
   sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw --force enable
   ```
   (Also open ports 80 and 443 in your provider's own firewall page if it has one.)

## 2. Install Docker (once)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER        # then log out and in again
docker --version && docker compose version
```

## 3. Copy the project to the server

The project folder must contain both `lecturekit/` and `server/` (the build needs both). From your own computer:

```bash
cd ~/Documents/Degree/Research/project
rsync -av --exclude '.venv' --exclude 'data' --exclude 'storage' --exclude 'logs' --exclude '.cache' \
      --exclude 'testing_result' --exclude 'dev_data' --exclude '.env' --exclude '__pycache__' \
      lecturekit server <user>@<ip>:~/app/
```
(Or `git clone` both repositories into `~/app/` on the server.) Do **not** copy any `.env` file: you create a new one on the server.

## 4. Configure

```bash
cd ~/app/server/deploy
cp .env.example .env
nano .env            # fill ANTHROPIC_API_KEY, JWT_SECRET, INVITE_CODE, DOMAIN (Ctrl+O, Enter, Ctrl+X to save)
openssl rand -hex 32 # use this output as JWT_SECRET
chmod 600 .env
mkdir -p data        # the database and all uploads live here (the container runs as user id 1000)
```
If your server user is not id 1000 (check with `id -u`), run `sudo chown -R 1000:1000 data`.

## 5. Build and start

```bash
docker compose up -d --build        # first time: 10 to 20 minutes (LibreOffice and the Whisper model are large)
docker compose ps                   # both services should be "running"/"healthy"
curl https://<your-domain>/health   # should print {"ok":true,...}
```
Caddy gets the HTTPS certificate by itself the first time (needs ports 80 and 443 open and the domain pointing at the server).

## 6. First checks

1. Register the first account (replace the values):
   ```bash
   curl -X POST https://<your-domain>/api/auth/register -H 'Content-Type: application/json' \
        -d '{"email":"you@example.com","password":"a long password","invite_code":"<INVITE_CODE>"}'
   ```
2. Run the smoke test from your own computer with the 14-minute test lecture and its slides:
   ```bash
   cd server && .venv/bin/python scripts/smoke.py --url https://<your-domain> --invite <INVITE_CODE> \
        --recording lecture14.mp4 --slides slides.pdf
   ```
   It prints each stage and the time; **write the total time in the table below**.
   For the live (chunk) path: add `--live --end 600` (needs `PYTHONPATH=../lecturekit`).
3. Look at the logs: `docker compose logs app | tail -50` and `grep -c sk-ant data/logs/*` (must print 0 for each file).

| Measurement (fill in on the real server) | Value |
|---|---|
| Server size | |
| 14-minute lecture with slides, whole file: seconds after upload until done | (laptop M3 Pro: about 190 s, ratio 0.22) |
| Same, live chunks (seconds after the last chunk) | |

## 7. Everyday commands

```bash
cd ~/app/server/deploy
docker compose logs -f app                       # watch the server
docker compose restart app                       # restart (unfinished jobs are marked failed: "server_restarted"; users retry)
docker compose exec app python -m server.admin list-users
docker compose exec app python -m server.admin set-password someone@example.com     # forgotten password
git pull && docker compose up -d --build         # update (or rsync again, then the same command)
docker compose down                              # stop everything (data stays in ./data)
```

## 8. Backups (do this before the viva)

Everything that matters is in `~/app/server/deploy/data` (database, uploads, results). Copy it away:

```bash
# once, by hand, from your own computer
scp -r <user>@<ip>:~/app/server/deploy/data ./lecturekit-backup-$(date +%F)
# or a daily copy on the server (crontab -e):
0 3 * * * tar czf ~/backup-$(date +\%F).tgz -C ~/app/server/deploy data && find ~ -maxdepth 1 -name 'backup-*.tgz' -mtime +7 -delete
```
Large lecture recordings are deleted from the server `KEEP_MEDIA_DAYS` (default 7) days after a lecture is done; notes, quiz and transcript stay.

## 9. If something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| `https://<domain>` does not load, Caddy log says "challenge failed" | the domain does not point at the server yet, or ports 80/443 are closed (also in the provider's firewall). Check with `curl http://<ip>`. |
| `app` keeps restarting | `docker compose logs app`: a missing `JWT_SECRET` (needs 32+ characters) or `INVITE_CODE` is the usual cause |
| Upload fails with 413 | file above `MAX_RECORDING_MB` (1500); raise it in `.env` **and** `max_size` in `Caddyfile`, then `docker compose up -d` |
| A lecture shows "The AI service failed" | Claude API key wrong or no credit: check the console; press retry in the app after fixing |
| "ffmpeg could not read this file" | the recording is damaged or has no audio track |
| Out of disk | `docker system prune -af` (images only) and delete old data you do not need; check `df -h` |
| Permission denied on `data/` | `sudo chown -R 1000:1000 data` |

## 10. Security checklist

- `.env` exists only on the server (`chmod 600`), is never committed, never copied by rsync (excluded above).
- Port 8000 is not published: only Caddy (80/443) is reachable from outside.
- Share the invite code only with people you want as users; change `INVITE_CODE` and `docker compose up -d` to close registration to newcomers.
- Daily limit per user: `DAILY_LIMIT` (default 5 lectures), because every lecture costs Claude credit. Keep auto-reload off in the Anthropic console and check the balance.

## 11. Acceptance checklist (manual, on the deployed server)

- [ ] `https://<domain>/health` ok, shows `ANTHROPIC_API_KEY: true` and `queue_length: 0`
- [ ] register with the invite code; a wrong code is refused; login works; logout (token discarded) and login again
- [ ] upload the 14-minute lecture with its slides; the stage progress moves (transcribe, align, filter, notes, quiz)
- [ ] read the notes; the quiz shows no answers; submit the quiz and see the score, correct answers and explanations
- [ ] export the notes as Markdown
- [ ] repeat with live chunks (`--live`)
- [ ] a second account cannot see the first account's lectures
- [ ] delete the session (its files disappear: `ls data/users/<id>/sessions`)
- [ ] `grep -c sk-ant data/logs/*` prints 0 for every file
