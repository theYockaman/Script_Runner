# Script Runner

A lightweight GUI-driven script runner that lets you add, edit, delete and run scripts either continuously or on a cron schedule. The project is intended to be run as a Docker image for easy deployment.

**Core features**
- **Add / Edit / Delete scripts:** CRUD operations for scripts and their metadata.
- **Run on schedule or continuously:** Support for cron-style schedules and continuous/background runs.
- **Execution isolation & logging:** Capture stdout/stderr per run and retain history.
- **Dockerized:** Build and run as a container for production deployment.

**What this README covers**
- Quick architecture and data model
- Example `Dockerfile` and `docker-compose.yml` usage
- API + UI examples for CRUD operations
- Security, persistence, and recommended next steps

**Architecture (suggested)**
- Frontend: single-page app (React / Vue / Svelte) that calls the backend REST API.
- Backend: small web service (Python Flask/FastAPI or Node Express) exposing REST endpoints to manage scripts and trigger runs.
- Scheduler: internal scheduler (APScheduler for Python, node-cron for Node) or rely on host cron. The scheduler reads enabled scripts and triggers execution according to their schedule.
- Executor: responsible for running commands, capturing logs, and returning status. Use a non-root user, limit resources, and optionally run in ephemeral containers for better isolation.
- Persistence: SQLite for a single-node small install. Use Postgres/MySQL for production.

Data model (simple)
- `script`:
	- `id` (int)
	- `name` (string)
	- `command` (string) — shell command or script path
	- `schedule` (string|null) — cron expression or null for continuous/manual runs
	- `enabled` (bool)
	- `env` (json/object) — optional environment variables for the run
	- `last_run_at` (datetime)
	- `created_at`, `updated_at`

Example REST endpoints (design)
- `GET /api/scripts` — list scripts
- `GET /api/scripts/:id` — get script
- `POST /api/scripts` — create script (JSON payload)
- `PUT /api/scripts/:id` — update script
- `DELETE /api/scripts/:id` — delete script
- `POST /api/scripts/:id/run` — trigger an immediate run
- `GET /api/scripts/:id/logs` — fetch run logs/history

API example (create a script)

```
curl -X POST http://localhost:8080/api/scripts \
	-H "Content-Type: application/json" \
	-d '{"name":"Nightly backup","command":"/app/scripts/backup.sh","schedule":"0 2 * * *","enabled":true}'
```

Trigger immediate run

```
curl -X POST http://localhost:8080/api/scripts/1/run
```

Minimal UI behavior
- Dashboard listing scripts and their next run
- Form to add/edit script (name, command, schedule, env, enabled)
- Per-script run history and log viewer
- Buttons for immediate run, enable/disable

Docker build & run (example)

Example `Dockerfile` (backend + scheduler; adapt for chosen stack):

```
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
USER 1000
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:8080", "--workers", "2"]
```

Build image

```
docker build -t script-runner:latest .
```

Run container

```
docker run --rm -p 8080:8080 \
	-v /path/to/scripts:/app/scripts \
	-v /path/to/data:/app/data \
	-e DATABASE_URL=sqlite:///app/data/data.db \
	--name script-runner script-runner:latest
```

Example `docker-compose.yml` (development)

```
version: '3.8'
services:
	app:
		image: script-runner:latest
		build: .
		ports:
			- 8080:8080
		volumes:
			- ./scripts:/app/scripts
			- ./data:/app/data
		environment:
			- DATABASE_URL=sqlite:///app/data/data.db
```

Scheduling notes
- Use an internal scheduler (recommended) so the UI can show next run times and the scheduler honors the `enabled` flag.
- For distributed/high-availability setups, use a persistent queue + worker pool (e.g., Redis + RQ/Celery) and a central schedule store.

Security & sandboxing
- Do not run commands as root. Use a dedicated, unprivileged user inside the container.
- Limit command capabilities: prefer executing script files rather than arbitrary commands if possible.
- For stronger isolation, run each script in a transient container (Docker-in-Docker or spawn `docker run --rm` with limited capabilities).
- Enforce resource limits (CPU, memory) at container runtime or via cgroups.

Persistence & logs
- Store run history and logs in the database and rotate/expire old logs.
- Expose log download or streaming endpoints to the UI.

Developer notes / scaffold
- Backend: implement REST API, scheduler, and executor. Use `APScheduler` (Python) or `node-cron` (Node) for schedule parsing and triggering.
- Frontend: simple SPA to call the API and show run history & logs.
- Tests: unit tests for scheduler triggers and executor behavior.

Suggested minimal file layout

- `Dockerfile` — example above
- `requirements.txt` or `package.json` — deps
- `app/` — backend code (API + scheduler + executor)
- `frontend/` — SPA code
- `scripts/` — example scripts to run
- `data/` — SQLite DB and logs when running locally

Next steps
- Implement backend API and scheduler (see endpoints above).
- Build the UI and wire up to the API.
- Add example `Dockerfile` and `docker-compose.yml` to the repo.

License
- Add a project license file if you plan to publish.

---

If you want, I can scaffold a minimal backend (FastAPI + APScheduler) and `Dockerfile` now and add them to the repo. Want me to create those files? 

Script authoring & conventions
------------------------------

Where to place scripts
- Place runnable scripts under a dedicated folder mounted into the container, e.g. `/app/scripts` in container and `./scripts` on the host.
- Keep scripts small and single-purpose. Name them clearly (e.g., `backup-daily.sh`, `sync-db.py`).

Script format
- Include a shebang on top (e.g., `#!/usr/bin/env bash` or `#!/usr/bin/env python3`).
- Ensure the script is executable (`chmod +x script.sh`) when mounted into the container.
- Use explicit paths (or set `WORKDIR`) and avoid reliance on interactive prompts.
- Exit with meaningful exit codes: `0` success, non-zero for failure.

Environment and secrets
- Prefer reading configuration and secrets from environment variables.
- When creating the script via the UI/API, store non-sensitive environment variables with the script record. For secrets, use a secure secret store or bind a file into the container.

Example bash script (`scripts/backup-daily.sh`)

```
#!/usr/bin/env bash
set -euo pipefail
echo "Starting backup at $(date --iso-8601=seconds)"
# example: tar and move to mounted backup folder
tar -czf /app/data/backups/backup-$(date +%Y%m%dT%H%M%S).tar.gz /app/data/to-backup
echo "Backup finished"
```

Example python script (`scripts/cleanup.py`)

```
#!/usr/bin/env python3
import os
from datetime import datetime

print(f"Cleanup started at {datetime.utcnow().isoformat()}Z")
# implement cleanup logic here
```

Adding scripts to the system
- Via UI: use the Add Script form (name, command, schedule, env, enabled). For `command` use the path where the script will be available inside the container, e.g. `/app/scripts/backup-daily.sh`.
- Via API: POST to `/api/scripts` with a JSON body (see earlier example).

Docker: frontend usage
----------------------

Single-container frontend (served by backend)
- Many simple deployments build frontend assets into the backend image and serve static files from the same container. Set `API_URL` to the backend's internal URL when building the frontend.

Separate frontend container
- Build the frontend into a static bundle and serve with a small webserver (nginx, httpd) or `serve`.
- Example `Dockerfile` for a static frontend (React/Vite):

```
FROM node:20 AS build
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM nginx:stable-alpine
COPY --from=build /src/dist /usr/share/nginx/html
ENV API_URL=http://backend:8080
```

Runtime: Tell the frontend where the backend is (via env var `API_URL` or a runtime-config JSON). When using `docker-compose`, set `API_URL` to `http://backend:8080` and ensure the service name matches.

Docker: backend usage
---------------------

Key environment variables
- `PORT` — port the backend listens on (default `8080`).
- `DATABASE_URL` — e.g., `sqlite:///app/data/data.db` or a Postgres DSN.
- `SCRIPTS_DIR` — location where scripts are stored inside the container (default `/app/scripts`).
- `LOG_DIR` — directory for run logs (default `/app/data/logs`).

Recommended bind mounts
- `-v $(pwd)/scripts:/app/scripts:ro` — your scripts (ro if you want the container to not modify them).
- `-v $(pwd)/data:/app/data` — DB and logs persistence.

Run backend locally (example)

```
docker build -t script-runner:latest .
docker run --rm -p 8080:8080 \
	-v ${PWD}/scripts:/app/scripts \
	-v ${PWD}/data:/app/data \
	-e DATABASE_URL=sqlite:///app/data/data.db \
	-e SCRIPTS_DIR=/app/scripts \
	--name script-runner script-runner:latest
```

Run backend + frontend with `docker-compose`

```
version: '3.8'
services:
	backend:
		build: .
		image: script-runner:latest
		ports:
			- 8080:8080
		volumes:
			- ./scripts:/app/scripts
			- ./data:/app/data
		environment:
			- DATABASE_URL=sqlite:///app/data/data.db
	frontend:
		build:
			context: .
			dockerfile: frontend/Dockerfile
		ports:
			- 3000:80
		environment:
			- API_URL=http://backend:8080
		depends_on:
			- backend
```

Debugging & logs
----------------
- Backend logs: `docker logs -f script-runner` (or `docker-compose logs -f backend`).
- Script run logs: fetch via the UI or `GET /api/scripts/:id/logs`.
- If a script fails, inspect the exit code and last stdout/stderr recorded in logs.

Safety checklist before enabling a script
- Verify the script runs locally in a minimal container matching production environment.
- Verify resource usage and add limits where needed (e.g., `--memory`, `--cpus`).
- Avoid long-running interactive commands; use non-interactive flags.

If you'd like, I can now scaffold a minimal FastAPI backend with APScheduler, an example frontend build, and add `Dockerfile` + `docker-compose.yml` to the repo. Shall I proceed? 
