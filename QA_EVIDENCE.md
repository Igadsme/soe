# QA evidence — production build on `main` @ 7b438ce

Run on the shared Linux QA box, Nov 16 2026, ~1:30 AM ET, against `main` at merge commit `7b438ce` (PR #6), using **dummy, non-secret QA values** in a local, uncommitted `.env` (session secret, admin password hash, DB password are throwaway QA strings). Nothing was deployed. Containers/volumes were started under compose project `soeqa` and torn down afterwards.

Environment: Docker 28.5.1, Compose v2.40.0, Python 3.13 venv (CI uses 3.12), Node 24 (CI uses 22).
Local-only build tweak: images were built with `build.network: host` and `INSTALL_SYSTEM_FFMPEG=false` (the box cannot reach Debian mirrors); the app then uses the bundled imageio-ffmpeg binary. CI builds the images with the default (system ffmpeg).

## 1. CI on `main` after each merge

| Merge | PR | Merge SHA | CI run on main | Result |
|---|---|---|---|---|
| Production build | #4 | `5fa6340` | [43812602285](https://github.com/Igadsme/soe/actions/runs/43812602285) | success (backend, frontend, containers) |
| Hardening | #5 | `a963d78` | [43815333295](https://github.com/Igadsme/soe/actions/runs/43815333295) | success |
| Shared rate limit | #6 | `7b438ce` | [43816706743](https://github.com/Igadsme/soe/actions/runs/43816706743) | success |

## 2. Test suite (fresh venv, same install commands as CI)

```
pip install -r backend/requirements.txt && pip install -e ".[dev]"
PYTHONPATH=backend pytest -q -rs --cov=backend/app backend/tests tests
-> 54 passed, 1 skipped (test_media_e2e.py: opt-in SOE_RUN_MEDIA_E2E=1), coverage 73%
ruff check backend/app                     -> All checks passed
pip-audit -r backend/requirements.txt      -> No known vulnerabilities found
SOE_DATABASE_URL=sqlite:////tmp/m.db alembic upgrade head -> 0001 -> 0002 -> 0003 applied
cd frontend && npm ci && npm run typecheck && npm run build -> OK (compiled successfully)
npm audit --omit=dev                       -> found 0 vulnerabilities
```
The frontend has no unit-test suite (`npm test` is not defined); it is covered by typecheck, build, and the browser check in section 5.

## 3. Production containers

```
cp <dummy QA env> .env
docker compose -p soeqa -f docker-compose.prod.yml config --quiet   -> OK
docker compose -p soeqa -f docker-compose.prod.yml build --no-cache -> exit 0
docker compose -p soeqa -f docker-compose.prod.yml up -d
docker compose -p soeqa -f docker-compose.prod.yml ps -a
```
```
api | soe-api:local | Up 45 seconds (healthy)
db | postgres:16-alpine | Up 58 seconds (healthy)
frontend | soe-frontend:local | Up 40 seconds (healthy)
migrate | soe-api:local | Exited (0) 46 seconds ago
redis | redis:7-alpine | Up 58 seconds (healthy)
worker | soe-api:local | Up 45 seconds (healthy)
```
- `migrate` ran `alembic upgrade head` (3 revisions) and exited 0 before `api`/`worker` started.
- `api`, `worker`, `frontend` run as non-root user `soe` (uid 10001).
- Container logs (153 lines) grepped for the QA secret, password, DB password and `soe_session=`: **0 matches**.

## 4. API end-to-end (`python e2e.py`, against http://localhost:8000 and :3000)

| Check | Command | Status | Result | Output (truncated) |
|---|---|---|---|---|
| Liveness | `GET /health` | 200 | PASS | {"status":"ok","service":"soe-api"} |
| Readiness (DB + migrations at head) | `GET /ready` | 200 | PASS | {"status":"ready","service":"soe-api"} |
| Security headers present | `GET /ready (headers)` | 200 | PASS | x-content-type-options=y, x-frame-options=y, content-security-policy=y, strict-transport-security=y, permissions-policy=y, x-request-id=y |
| Frontend serves | `GET http://localhost:3000/` | 200 | PASS | 7088 bytes html |
| Unauthenticated request rejected | `GET /api/v1/overview (no cookie)` | 401 | PASS | {"detail":"authentication required"} |
| API docs disabled in production | `GET /docs` | 404 | PASS | {"detail":"Not Found"} |
| Login wrong password rejected | `POST /api/v1/auth/login (bad pw)` | 401 | PASS | {"detail":"invalid credentials"} |
| Login with valid credentials | `POST /api/v1/auth/login` | 200 | PASS | {"authenticated":true,"username":"operator"} |
| Session cookie flags | `Set-Cookie soe_session` | 200 | PASS | HttpOnly=True Secure=True SameSite=True |
| Authenticated request (session) | `GET /api/v1/auth/me` | 200 | PASS | {"username":"operator"} |
| Authenticated overview | `GET /api/v1/overview` | 200 | PASS | {"environment":"production","demo_mode":false,"counts":{"sources":0,"clips":0,"workflows":0},"revenue_cents":0,"expense_cents":0} |
| Tampered signature rejected | `GET /api/v1/auth/me (tampered sig)` | 401 | PASS | {"detail":"invalid or expired session"} |
| Tampered payload (username swap) rejected | `GET /api/v1/auth/me (payload edited)` | 401 | PASS | {"detail":"invalid or expired session"} |
| Expired (validly signed) session rejected | `GET /api/v1/auth/me (expired, re-signed with QA secret)` | 401 | PASS | {"detail":"invalid or expired session"} |
| Mutation without CSRF header rejected | `POST /api/v1/demo/pipeline (no X-CSRF-Token)` | 403 | PASS | {"detail":"CSRF validation failed"} |
| Demo pipeline disabled in production | `POST /api/v1/demo/pipeline (CSRF ok)` | 404 | PASS | {"detail":"not found"} |
| Path traversal media_path rejected | `POST /api/v1/sources media_path=/etc/passwd` | 422 | PASS | {"detail":[{"loc":["body","media_path"],"type":"value_error","msg":"invalid input"}]} |
| Upload with bad magic bytes rejected | `POST /api/v1/sources/upload (text as mp4)` | 415 | PASS | {"detail":"invalid media signature"} |
| Upload authorized source video | `POST /api/v1/sources/upload (10s mp4)` | 200 | PASS | {"source_id":1,"status":"rights_review","bytes":3504820,"filename":"upload-cb2ab551e95841e4bee131b3bfc0c522.mp4"} |
| List sources | `GET /api/v1/sources` | 200 | PASS | [{"platform":"upload","external_id":"upload-cb2ab551e95841e4bee131b3bfc0c522","title":"QA original test video","url":"https://local.soe/upload","media_path":"/a |
| Process blocked before rights approval | `POST /api/v1/sources/1/process (no rights)` | 404 | PASS | {"detail":"source or rights record not found"} |
| Rights approval | `POST /api/v1/sources/1/rights` | 201 | PASS | {"source_id":1,"status":"authorized","can_publish":true} |
| Process: ingest, transcribe (Whisper tiny.en), rank, render | `POST /api/v1/sources/1/process (6s)` | 200 | PASS | {"workflow_id":1,"source_id":1,"state":"edited","probe":{"duration":8.7,"width":1280,"height":720,"has_audio":true},"transcript_segments":3,"clips_generated":3} |
| List rendered clips | `GET /api/v1/clips` | 200 | PASS | 3 clips |
| Download rendered clip MP4 | `GET /api/v1/clips/1/download` | 200 | PASS | 586305 bytes, type=video/mp4, ftyp=True |
| Publishing gated off by default | `POST /api/v1/clips/1/publish` | 409 | PASS | {"detail":"publishing is disabled by SOE_PUBLISHING_ENABLED"} |
| Live discovery without provider key fails safely | `POST /api/v1/discovery/search (no YouTube key)` | 503 | PASS | {"detail":"service temporarily unavailable"} |
| Read /api/v1/publications | `GET /api/v1/publications` | 200 | PASS | [] |
| Read /api/v1/analytics | `GET /api/v1/analytics` | 200 | PASS | {"verified":[],"unverified":[]} |
| Read /api/v1/revenue | `GET /api/v1/revenue` | 200 | PASS | {"entries":[],"verified_only":true} |
| Read /api/v1/agents | `GET /api/v1/agents` | 200 | PASS | [{"name":"discovery","status":"blocked","detail":"fixture mode only","queued_workflows":0},{"name":"rights","status":"ready","detail":"fail-closed policy active |
| Read /api/v1/settings | `GET /api/v1/settings` | 200 | PASS | {"environment":"production","demo_mode":false,"whisper_model_size":"tiny.en","publishing_enabled":false,"max_upload_bytes":2000000000,"max_clip_seconds":180,"co |
| Logout | `POST /api/v1/auth/logout` | 200 | PASS | {"authenticated":false} |
| Old session token rejected after logout (server-side revocation) | `GET /api/v1/auth/me (pre-logout cookie)` | 401 | PASS | {"detail":"invalid or expired session"} |
| Login throttling (6 rapid bad attempts) | `POST /api/v1/auth/login x6` | 429 | PASS | codes=[401, 401, 401, 401, 401, 429] |

**35/35 passed.** The expired-token check builds a token whose expiry is in the past and signs it with the dummy QA secret, so only the expiry check can reject it.

## 5. Browser (headless Chrome via Playwright, system Chrome binary)

```
PASS UI loads; unauthenticated shows sign-in prompt ['Unable to load API data. Sign in or check the API connection.']
PASS UI sign-in loads overview cards ['Sources discovered\n\n1\nVerified source', 'Clips generated\n\n3\nVerified source', 'Workflow runs\n\n1\nVerified source', 'Verified revenue\n\n$0.00\nVerified source']
PASS UI authenticated Settings panel 
PASS UI sign-out 
PASS console errors (expected 401s before login only) ['Failed to load resource: the server responded with a status of 401 (Unauthorized)', 'Failed to load resource: the server responded with a status of 401 (Unauthorized)', 'Failed to load resource: the server responded with a status of 401 (Unauthorized)', 'Failed to load resource: the server responded with a status of 404 (Not Found)']
```
The 401s come from the dashboard loading before sign-in. The 404 is `/favicon.ico`.

## 6. Not verified (needs real accounts or credentials)

- Live YouTube/Twitch discovery: no API keys. Verified only that it fails safely (503).
- Publishing to YouTube or other platforms: the OAuth publishing client isn't implemented and publishing is off by default. Verified only the 409 gate.
- HTTPS/TLS termination, HSTS over real TLS, cookie `Secure` behaviour over a real domain: no host or certificate.
- Backups: `scripts/backup_db.sh` and `restore_db.sh` were syntax-checked but not run against off-site storage.
- Multi-host scaling, load testing, penetration testing, and the opt-in `SOE_RUN_MEDIA_E2E` pytest. That test needs macOS `say` fixtures; the container E2E above covered the real Whisper transcription and render path instead.

## Teardown
`docker compose -p soeqa -f docker-compose.prod.yml down -v` removed only the `soeqa` containers, network and volumes.

## 7. Production-readiness follow-up verification (2026-11-17)

| PR | Topic | Local evidence |
|---|---|---|
| #11 | Login backoff, shared Redis client, backup encryption, metrics/private access | 140 passed, 3 skipped; 97.43% coverage; green GitHub CI |
| #12 | P1-A processing deadlines and killable media subprocesses | 142 passed, 3 skipped; 96.77% coverage; Ruff/pip-audit passed; green GitHub CI |
| #13 | P1-B rerun guard, publication FK, schema hardening and folder cleanup | 144 passed, 3 skipped; 96.67% coverage; green GitHub CI |
| #14 | CI/Postgres/Redis/coverage and ingest copy reduction | 144 passed, 3 skipped; 96.90% coverage; frontend 22 Vitest tests/build passed; green GitHub CI |

Relevant commands passed:

```
PYTHONPATH=backend pytest -q
ruff check backend/app
pip-audit -r backend/requirements.txt
alembic upgrade head
alembic check
cd frontend && npm ci && npx vitest run && npm run build
```

## 8. NOT TESTED — local production Compose

The Docker Desktop engine socket remained unavailable after a bounded quit/restart/ping retry.
Build output previously reported read-only Docker Desktop storage (`metadata_v2.db`/`containerdmeta.db`).
Therefore only these runtime-container items remain genuinely NOT TESTED:

- Caddy local TLS issuance and real-Https certificate verification
- Compose startup/migration as running containers
- Real Caddy 413/metrics/security-header behavior
- Container-to-container login/upload/process/download flow
- Backup restore inside fresh Compose database
- Deploy/rollback against running Compose services

The following equivalent executable local checks passed without Docker or real credentials:

| Item | Result |
|---|---|
| HTTPS security headers over ASGI production middleware | PASS |
| Authentication and CSRF; unauthorized routes return 401/403 | PASS |
| 0-byte/invalid upload handling and partial-file cleanup | PASS |
| Rights evidence and processing authorization | PASS |
| Metrics token/private-network enforcement and forwarded-header spoof rejection | PASS |
| ASGI upload size and 413 body guard | PASS |
| Upload, rights, process, clips and MP4 download paths | PASS |
| Hung/deadline lease stop, renewal stop, retry/fail and next-job behavior | PASS |
| Backup and relative restore script argument/syntax tests | PASS |
| Deploy/rollback script argument checks | PASS |
| Health and readiness/migration-head/Redis checks | PASS |

Targeted security/operations command:

```
pytest -o addopts='' -q backend/tests/test_hardening.py backend/tests/test_observability.py backend/tests/test_jobs.py backend/tests/test_deploy_scripts.py
-> 51 passed
```

No cloud resources, deployments, real credentials, real media, or real user data were used.
