Quick start — build and run with Docker

1) Build images with docker-compose

```bash
docker compose build
docker compose up -d
```

2) Open the frontend at http://localhost:3015 and the backend API at http://localhost:8005/api
   (ports are set in `docker-compose.yml`). Sign in with an account from `.env` (see Login below).

3) Place additional scripts in the `scripts/` folder and add them via the UI or the API using the container path (e.g. `/app/scripts/backup-daily.sh`).

4) To view live backend logs:

```bash
docker compose logs -f backend
```

Login
-----

The UI and every `/api/` route require a login. Accounts live only in `.env` next to
`docker-compose.yml`; there is no user management in the UI or API, and every account has
full access.

1. Copy `.env.example` to `.env` (gitignored) if it does not exist.
2. Set `SCRIPT_RUNNER_USERS` to comma-separated `user:password` pairs. Passwords may contain
   `:` but not `,`; single-quote the whole value if a password contains `$`, `#`, spaces or
   quotes.
3. Set `SECRET_KEY` to a long random string (`python -c "import secrets; print(secrets.token_hex(32))"`).
   Without it a random key is generated at startup and everyone is logged out on each restart.
4. Run `docker compose up -d` after any change. A plain `docker compose restart` does not re-read `.env`.

Sessions are signed cookies valid for `SESSION_MAX_AGE_HOURS` (default one week). To revoke a
user, remove them from `SCRIPT_RUNNER_USERS` and run `docker compose up -d`; to log everyone out,
change `SECRET_KEY`. Logging out only clears the browser cookie. Set `COOKIE_SECURE=true` only
behind https. If you run uvicorn outside Docker, export these variables yourself; nothing in the
Python code reads `.env`.

Supported languages
-------------------

The backend image ships interpreters for Bash, Python, Node.js, TypeScript (via `tsx`),
PowerShell 7, Ruby, Perl and PHP. `GET /api/languages` lists them and whether each
interpreter is installed.

- **File path / command:** point at a file in `scripts/` (relative paths resolve from that
  folder). The interpreter is chosen from the extension (`.sh .py .js .ts .ps1 .rb .pl .php`)
  unless you pick a language explicitly. Unknown extensions run as a plain shell command.
- **Inline code:** paste code in the UI and pick a language. It is written to
  `scripts/.inline/` for the duration of each run and removed afterwards.
- **Environment variables:** `KEY=VALUE` lines on the script are passed to the process.

Running the tests
-----------------

The tests expect the container layout (`/app/scripts`, Linux interpreters), so run them
inside the backend image against a throwaway database:

```powershell
docker compose run --rm -e DATABASE_URL=sqlite:////tmp/test.db -e PYTHONPATH=/app `
  -v "${PWD}\tests:/app/tests" -v "${PWD}\frontend:/app/frontend:ro" backend pytest -q /app/tests
```
