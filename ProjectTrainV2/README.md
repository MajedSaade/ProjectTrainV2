# Semiconductor Telemetry Pipeline

Multi-threaded telemetry processing engine with Redis persistence, Docker containerization, chaos validation, and Jenkins CI/CD (build → push → deploy).

---

## Repository Layout

```
ProjectTrainV2/
├── app/                          # Python application
│   ├── __init__.py
│   └── pipeline.py               # Producers, consumers, Redis, chaos, heartbeat
├── compose/
│   ├── docker-compose.yml        # Local dev + CI validation (builds image)
│   └── docker-compose.deploy.yml # Production deploy (pulls from registry)
├── docker/
│   └── entrypoint.sh             # Redis readiness gate + exec PID 1
├── scripts/
│   ├── run_validation.sh         # Automated chaos + integrity tests
│   ├── deploy.sh                 # Pull image and deploy local stack
│   └── notify.py                 # Jenkins email notifications
├── Dockerfile                    # Multi-stage non-root runtime image
├── Jenkinsfile                   # CI/CD pipeline (webhook-triggered)
├── requirements.txt
└── README.md
```

**Not in this repo:** Jenkins infrastructure lives separately at `/home/majed/AntiGround/ProjectTrain/jenkins_docker`.

---

## Architecture

```
Producers (×3) → Bounded Queue (100) → Consumers (×4)
                         │                    │
                         │         ┌──────────┼──────────┐
                         │         ▼          ▼          ▼
                         │    Redis Hashes  Error Ch.  Heartbeat
                         │    (TOOL_xx)     (500/corrupt) /tmp/app_heartbeat
                         ▼
                   datastore (redis:7-alpine, internal network)
```

---

## Quick Start (Local)

```bash
# Validate everything (chaos mode, Redis, heartbeat, graceful shutdown)
./scripts/run_validation.sh

# Run stack locally (build from source)
docker compose -f compose/docker-compose.yml up --build

# Inspect Redis
docker compose -f compose/docker-compose.yml exec datastore redis-cli HGETALL TOOL_01
```

---

## Jenkins CI/CD Guide (ngrok + GitHub Webhook)

### Prerequisites

| Item | Location / Value |
|---|---|
| Jenkins stack | `/home/majed/AntiGround/ProjectTrain/jenkins_docker` |
| Jenkins URL | Your ngrok URL (e.g. `https://abc123.ngrok-free.app`) |
| GitHub webhook | Already connected to ngrok → Jenkins |
| Agent | `agent-1` (online, with Docker socket) |
| Docker Hub repo | `majedsaade/telemetry-pipeline` |

### 1. Start Jenkins (if not running)

```bash
cd /home/majed/AntiGround/ProjectTrain/jenkins_docker
docker compose up -d
```

Confirm:
- Jenkins UI loads at your ngrok URL
- **Manage Jenkins → Nodes** → `agent-1` is **online**

### 2. Start ngrok (if not running)

```bash
ngrok http 8080
```

Use the HTTPS forwarding URL as your Jenkins public address.

### 3. Verify GitHub webhook

On your GitHub repo → **Settings → Webhooks**:

| Field | Value |
|---|---|
| Payload URL | `https://<your-ngrok-id>.ngrok-free.app/github-webhook/` |
| Content type | `application/json` |
| Events | Push events |
| Active | ✓ |

In Jenkins → **Manage Jenkins → System → GitHub**:

- GitHub Server configured with your ngrok hook URL
- "Manage hooks" or test connection shows green

### 4. Configure Jenkins credentials

**Manage Jenkins → Credentials → System → Global:**

| ID | Type | Purpose |
|---|---|---|
| `dockerhub-registry-Credentials` | Username/password | Docker Hub push/pull |
| `smtp-credentials` | Username/password | Gmail app password |
| `EmailToSend` | Secret text | Notification recipient |

### 5. Create the Pipeline job

1. **Dashboard → New Item**
2. Name: `telemetry-pipeline`
3. Type: **Pipeline** → OK
4. **Build Triggers:** ensure **GitHub hook trigger for GITScm polling** is checked
5. **Pipeline:**
   - Definition: **Pipeline script from SCM**
   - SCM: **Git**
   - Repository URL: your GitHub repo URL
   - Credentials: (if private repo)
   - Branch: `*/main`
   - Script Path: `Jenkinsfile`
6. Save

### 6. Push code to trigger the pipeline

```bash
cd /home/majed/AntiGround/ProjectTrainV2/ProjectTrainV2
git add .
git commit -m "Organize repo and configure webhook pipeline"
git push origin main
```

GitHub webhook fires → Jenkins receives push → pipeline starts on `agent-1`.

### 7. What the pipeline does

| Stage | Action |
|---|---|
| **Checkout** | Clone latest code from GitHub |
| **Validate** | `./scripts/run_validation.sh` (chaos test, Redis, SIGINT) |
| **Build and Push** | `docker build` → push `majedsaade/telemetry-pipeline:$BUILD_NUMBER` and `:latest` |
| **Deploy** | `./scripts/deploy.sh` pulls `:latest` and starts `compose/docker-compose.deploy.yml` |
| **Post** | Email notification (SUCCESS/FAILURE) |

### 8. Verify deployment after a green build

```bash
docker compose -f compose/docker-compose.deploy.yml ps
docker compose -f compose/docker-compose.deploy.yml logs -f telemetry-app
docker compose -f compose/docker-compose.deploy.yml exec datastore redis-cli HGETALL TOOL_01
```

### 9. Manual build (without push)

In Jenkins → `telemetry-pipeline` → **Build Now**

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Webhook received but no build | Check job has **GitHub hook trigger** enabled; verify ngrok is running |
| `403` on webhook | Jenkins CSRF / ngrok URL mismatch — confirm payload URL ends with `/github-webhook/` |
| Agent offline | Update `JENKINS_SECRET` in `jenkins_docker/.env`, restart `agent-1` |
| Docker permission denied | Set correct `DOCKER_GROUP_ID` in `jenkins_docker/.env` |
| Push to Docker Hub fails | Verify `dockerhub-registry-Credentials` |
| Email not sent | Verify `smtp-credentials` and `EmailToSend` |
| Deploy fails | Ensure `majedsaade/telemetry-pipeline` repo exists on Docker Hub |

---

## Environment Variables

| Variable | Default | Used by |
|---|---|---|
| `REDIS_HOST` | `datastore` | pipeline.py, entrypoint.sh |
| `REDIS_PORT` | `6379` | pipeline.py, entrypoint.sh |
| `RUN_DURATION_SECONDS` | `10` (dev) / `3600` (deploy) | pipeline.py |
| `ENABLE_CHAOS` | `false` | pipeline.py |
| `HEARTBEAT_PATH` | `/tmp/app_heartbeat` | pipeline.py, healthcheck |

---

## Design Decisions

- **Bounded queue (100):** Backpressure protection against OOM under heavy load
- **Granular locking:** Lock only sum/count updates, not I/O or parsing
- **exec entrypoint:** Python becomes PID 1 for proper `SIGTERM`/`SIGINT` handling
- **Disk heartbeat:** Liveness probe for non-HTTP background workers
- **Separate compose files:** `docker-compose.yml` builds locally for CI; `docker-compose.deploy.yml` pulls from registry for deploy
