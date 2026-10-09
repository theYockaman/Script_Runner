import asyncio
import base64
import glob
import hashlib
import hmac
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Dict, List, NamedTuple, Optional, Tuple
import pytz

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, root_validator
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String,
                        Text, create_engine, inspect, text)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# Config
DB_PATH = os.environ.get("DATABASE_URL", "sqlite:///app/data/data.db")
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/app/scripts")
CENTRAL_TZ = pytz.timezone('America/Chicago')

# Auth: credentials come only from the environment (.env via docker compose), never from the UI/API.
SESSION_COOKIE = "script_runner_session"
AUTH_PUBLIC_PATHS = frozenset({"/api/auth/login", "/api/auth/status", "/api/auth/logout"})
AUTH_GUARDED_EXTRA_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})
LOGIN_FAIL_DELAY = 0.5  # seconds added to a failed login; awaited, so it never blocks other requests

# If using a SQLite file URL, ensure the parent directory exists before creating the engine.
if DB_PATH.startswith("sqlite:///"):
    # sqlite:///path/to/file -> path/to/file
    sqlite_file = DB_PATH.replace("sqlite:///", "", 1)
    sqlite_dir = os.path.dirname(sqlite_file) or "/"
    try:
        os.makedirs(sqlite_dir, exist_ok=True)
    except Exception:
        # best-effort; engine creation will surface any errors
        pass

engine = create_engine(DB_PATH, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
metadata = MetaData()
Base = declarative_base()


class Script(Base):
    __tablename__ = "scripts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    # File path / shell command. Stored as "" when the script is inline code
    # (the column is NOT NULL in existing databases and SQLite cannot relax that).
    command = Column(Text, nullable=False)
    schedule = Column(String(100), nullable=True)
    enabled = Column(Boolean, default=True)
    env = Column(Text, nullable=True)
    # Language id from LANGUAGES; NULL means "auto-detect from the file extension".
    language = Column(String(32), nullable=True)
    # Inline source code; when set it is written to a temp file under SCRIPTS_DIR/.inline per run.
    code = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(CENTRAL_TZ))
    updated_at = Column(DateTime, default=lambda: datetime.now(CENTRAL_TZ), onupdate=lambda: datetime.now(CENTRAL_TZ))


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(CENTRAL_TZ))
    finished_at = Column(DateTime, nullable=True)
    exit_code = Column(Integer, nullable=True)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)


def ensure_schema(bind) -> List[str]:
    """Add any Script columns missing from an existing `scripts` table.

    `create_all` only creates missing tables, never missing columns, so databases
    created before a column existed need an additive ALTER TABLE. Returns the
    names of the columns added (empty when the schema was already current).
    """
    existing = {c["name"] for c in inspect(bind).get_columns("scripts")}
    added: List[str] = []
    with bind.begin() as conn:
        for col in Script.__table__.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(dialect=bind.dialect)
            conn.execute(text(f"ALTER TABLE scripts ADD COLUMN {col.name} {col_type}"))
            added.append(col.name)
    if added:
        print(f"[SCHEMA] Added missing column(s) to scripts: {', '.join(added)}")
    return added


Base.metadata.create_all(bind=engine)
ensure_schema(engine)

app = FastAPI(title="Script Runner")
scheduler = BackgroundScheduler()
scheduler.start()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# Users are read from SCRIPT_RUNNER_USERS and every user has full access.
# Sessions are stateless: a signed cookie "b64url(username).issued.hmac" whose
# alphabet is [A-Za-z0-9_.-], so it is never quoted in the Set-Cookie header.

def parse_users(spec: Optional[str]) -> Dict[str, str]:
    """Parse `user:password,user2:password2` (newlines also separate entries).

    Split on the first ':' so passwords may contain ':' but not ','. Surrounding
    whitespace is trimmed. Invalid entries are reported (never with the password)
    and skipped; a repeated username keeps the last password.
    """
    users: Dict[str, str] = {}
    for raw in re.split(r"[,\n]", spec or ""):
        entry = raw.strip()
        if not entry:
            continue
        username, sep, password = entry.partition(":")
        username, password = username.strip(), password.strip()
        if not sep or not username or not password:
            print(f"[AUTH] Ignoring invalid SCRIPT_RUNNER_USERS entry for {username or '?'!r} (expected user:password)")
            continue
        if username in users:
            print(f"[AUTH] Duplicate user {username!r} in SCRIPT_RUNNER_USERS; keeping the last password")
        users[username] = password
    return users


def _load_secret_key() -> str:
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    print("[AUTH] WARNING: SECRET_KEY is not set; using a random key, so everyone is logged out on each restart.")
    return secrets.token_hex(32)


def _load_session_max_age() -> int:
    """Session lifetime in seconds from SESSION_MAX_AGE_HOURS (default one week)."""
    raw = os.environ.get("SESSION_MAX_AGE_HOURS", "").strip()
    hours = 168.0
    if raw:
        try:
            hours = float(raw)
            if hours <= 0:
                raise ValueError(raw)
        except ValueError:
            print(f"[AUTH] WARNING: invalid SESSION_MAX_AGE_HOURS {raw!r}; using 168")
            hours = 168.0
    return int(hours * 3600)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


USERS: Dict[str, str] = parse_users(os.environ.get("SCRIPT_RUNNER_USERS"))
SECRET_KEY: str = _load_secret_key()
SESSION_MAX_AGE: int = _load_session_max_age()
COOKIE_SECURE: bool = _env_flag("COOKIE_SECURE", False)

if USERS:
    print(f"[AUTH] Configured users: {', '.join(sorted(USERS))}")
else:
    print("[AUTH] WARNING: no users configured. Set SCRIPT_RUNNER_USERS=user:password in .env; nobody can log in until then.")


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_token(username: str, issued: Optional[int] = None) -> str:
    """Signed, cookie-safe token. `issued` is overridable so tests can craft old tokens."""
    issued = int(time.time()) if issued is None else int(issued)
    payload = f"{_b64u_encode(username.encode('utf-8'))}.{issued}"
    return f"{payload}.{_sign(payload)}"


def verify_session_token(token: Optional[str]) -> Optional[str]:
    """Username for a valid, unexpired token whose user still exists; otherwise None. Never raises."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    encoded, issued_text, signature = parts
    expected = _sign(f"{encoded}.{issued_text}")
    # Compare as bytes: str compare_digest raises TypeError on non-ASCII input.
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        return None
    try:
        issued = int(issued_text)
        username = _b64u_decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    age = int(time.time()) - issued
    if age < 0 or age > SESSION_MAX_AGE:
        return None
    if username not in USERS:
        return None
    return username


def current_user(request: Request) -> Optional[str]:
    return verify_session_token(request.cookies.get(SESSION_COOKIE))


def _password_ok(username: str, password: str) -> bool:
    """Constant-time comparison that also works for non-ASCII passwords; False when no users exist."""
    expected = USERS.get(username)
    known = expected is not None
    expected_digest = hashlib.sha256((expected or "").encode("utf-8")).digest()
    provided_digest = hashlib.sha256(password.encode("utf-8")).digest()
    return hmac.compare_digest(provided_digest, expected_digest) and known


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, path="/", httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=COOKIE_SECURE)


def _is_guarded_path(path: str) -> bool:
    if path in AUTH_PUBLIC_PATHS:
        return False
    return path.startswith("/api/") or path in AUTH_GUARDED_EXTRA_PATHS


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Require a valid session cookie for every API route except the auth endpoints."""
    if _is_guarded_path(request.url.path) and current_user(request) is None:
        # Return (never raise) from middleware: exceptions here would become 500s.
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

# id -> {label, extensions (the first one is used for inline files), argv prefix}
LANGUAGES: Dict[str, dict] = {
    "bash":       {"label": "Bash",       "extensions": [".sh", ".bash"],        "argv": ["bash"]},
    "python":     {"label": "Python",     "extensions": [".py"],                 "argv": ["python", "-u"]},
    "node":       {"label": "Node.js",    "extensions": [".js", ".mjs", ".cjs"], "argv": ["node"]},
    "typescript": {"label": "TypeScript", "extensions": [".ts", ".mts"],         "argv": ["tsx"]},
    "powershell": {"label": "PowerShell", "extensions": [".ps1"],                "argv": ["pwsh", "-NoProfile", "-NonInteractive", "-File"]},
    "ruby":       {"label": "Ruby",       "extensions": [".rb"],                 "argv": ["ruby"]},
    "perl":       {"label": "Perl",       "extensions": [".pl"],                 "argv": ["perl"]},
    "php":        {"label": "PHP",        "extensions": [".php"],                "argv": ["php"]},
}


def inline_dir() -> str:
    """Directory holding per-run temp files for inline scripts (read at call time so tests can patch SCRIPTS_DIR)."""
    return os.path.join(SCRIPTS_DIR, ".inline")


def normalize_newlines(s: str) -> str:
    """CRLF / CR -> LF, and guarantee a trailing newline (a shebang line ending in CR breaks bash)."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if s and not s.endswith("\n"):
        s += "\n"
    return s


def parse_env_text(env_text: Optional[str]) -> Dict[str, str]:
    """Parse KEY=VALUE lines.

    Blank lines, `#` comments and lines without `=` are ignored. An `export ` prefix
    and one layer of matching quotes around the value are stripped.
    """
    result: Dict[str, str] = {}
    for raw in (env_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def detect_language(path: str) -> Optional[str]:
    """Language id for a file path based on its extension, or None when unknown."""
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        return None
    for lang, spec in LANGUAGES.items():
        if ext in spec["extensions"]:
            return lang
    return None


def interpreter_available(lang: str) -> bool:
    return shutil.which(LANGUAGES[lang]["argv"][0]) is not None


def _missing_interpreter_message(lang: str) -> str:
    exe = LANGUAGES[lang]["argv"][0]
    label = LANGUAGES[lang]["label"]
    return f"Interpreter '{exe}' for {label} is not installed in the backend image."


def validate_script_fields(command: Optional[str], code: Optional[str], language: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    """Normalise and cross-check the three "what to run" fields.

    Returns (command, code, language) where command is "" for inline scripts,
    code is LF-normalised or None, and language is a LANGUAGES id or None (auto).
    Raises ValueError with a user-facing message when the combination is invalid.
    """
    command = (command or "").strip()
    code = normalize_newlines(code) if code and code.strip() else None
    language = (language or "").strip().lower() or None
    if language == "auto":
        language = None
    if language is not None and language not in LANGUAGES:
        valid = ", ".join(LANGUAGES)
        raise ValueError(f"Unknown language {language!r}. Valid languages: {valid}")
    if command and code:
        raise ValueError("Provide either a command/file path or inline code, not both")
    if not command and not code:
        raise ValueError("Provide a command/file path or inline code")
    if code and language is None:
        raise ValueError("Inline code requires an explicit language (auto-detect only works for file paths)")
    return command, code, language


def inline_script_path(script_id: int, run_id: int, lang: str) -> str:
    ext = LANGUAGES[lang]["extensions"][0]
    return os.path.join(inline_dir(), f"script_{script_id}_run_{run_id}{ext}")


def write_inline_script(script: "Script", run_id: int) -> str:
    """Write the script's inline code to a per-run file and return its path."""
    path = inline_script_path(script.id, run_id, script.language)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(normalize_newlines(script.code or ""))
    return path


def cleanup_inline_files(script_id: int) -> None:
    """Best-effort removal of every per-run inline file belonging to a script."""
    for p in glob.glob(os.path.join(inline_dir(), f"script_{script_id}_*")):
        try:
            os.remove(p)
        except OSError:
            pass


class Resolved(NamedTuple):
    args: object                      # List[str] when shell is False, the raw command string when shell is True
    shell: bool
    cwd: str
    lang: Optional[str]
    cleanup_path: Optional[str]       # per-run inline file to delete afterwards
    error: Optional[Tuple[int, str]]  # (exit_code, message) when the run cannot start at all


def _error(lang: Optional[str], code: int, message: str) -> Resolved:
    return Resolved(None, False, SCRIPTS_DIR, lang, None, (code, message))


def resolve_command(script: "Script", run_id: int) -> Resolved:
    """Decide how to execute a script.

    - Inline code: written to a per-run file and run with the explicit language's interpreter.
    - File/command with a known (explicit or extension-detected) language:
      `argv_prefix + [file] + args` without a shell; the file must exist.
    - Anything else: the raw command string through the shell, as before.
    """
    lang = script.language or None

    if script.code and script.code.strip():
        if not lang or lang not in LANGUAGES:
            return _error(lang, 2, "Inline code requires a valid language")
        if not interpreter_available(lang):
            return _error(lang, 127, _missing_interpreter_message(lang))
        path = write_inline_script(script, run_id)
        return Resolved(LANGUAGES[lang]["argv"] + [path], False, SCRIPTS_DIR, lang, path, None)

    command = (script.command or "").strip()
    if not command:
        return _error(lang, 2, "Script has neither a command nor inline code")
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return _error(lang, 2, f"Could not parse command: {e}")
    if not tokens:
        return _error(lang, 2, "Script has neither a command nor inline code")

    first = os.path.abspath(os.path.join(SCRIPTS_DIR, tokens[0]))
    if lang is None:
        lang = detect_language(tokens[0])

    if lang is not None:
        if lang not in LANGUAGES:
            return _error(lang, 2, f"Unknown language {lang!r}")
        if not os.path.isfile(first):
            return _error(lang, 2, f"Script file not found: {first}")
        if not interpreter_available(lang):
            return _error(lang, 127, _missing_interpreter_message(lang))
        convert_line_endings(first)
        args = LANGUAGES[lang]["argv"] + [first] + tokens[1:]
        return Resolved(args, False, os.path.dirname(first), lang, None, None)

    # Unknown extension / plain command: today's behaviour, through the shell.
    script_dir = os.path.dirname(first)
    cwd = script_dir if os.path.isdir(script_dir) else SCRIPTS_DIR
    return Resolved(command, True, cwd, None, None, None)


_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([^'.]+)")


def _missing_python_module(stderr: str) -> Optional[str]:
    m = _MISSING_MODULE_RE.search(stderr or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class ScriptCreate(BaseModel):
    name: str
    command: Optional[str] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = True
    env: Optional[str] = None
    language: Optional[str] = None
    code: Optional[str] = None

    @root_validator(skip_on_failure=True)
    def _check_source(cls, values):
        command, code, language = validate_script_fields(
            values.get("command"), values.get("code"), values.get("language")
        )
        values["command"] = command
        values["code"] = code
        values["language"] = language
        return values


class ScriptUpdate(BaseModel):
    # Partial patch: only fields present in the request are applied. Cross-field
    # validation happens in update_script after merging onto the stored row.
    name: Optional[str] = None
    command: Optional[str] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = None
    env: Optional[str] = None
    language: Optional[str] = None
    code: Optional[str] = None


class ScriptOut(BaseModel):
    id: int
    name: str
    command: str
    schedule: Optional[str]
    enabled: bool
    env: Optional[str] = None
    language: Optional[str] = None
    code: Optional[str] = None

    class Config:
        orm_mode = True


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def schedule_script(db: Session, script: Script):
    """(Re)schedules a single script."""
    print(f"[SCHEDULE] Attempting to schedule script {script.id}: {script.name}")
    print(f"[SCHEDULE] Enabled: {script.enabled}, Schedule: {script.schedule}")

    # always remove before adding
    if scheduler.get_job(str(script.id)):
        print(f"[SCHEDULE] Removing existing job for script {script.id}")
        scheduler.remove_job(str(script.id))

    if script.enabled and script.schedule:
        try:
            # Support both 5-part and 6-part cron expressions
            parts = script.schedule.split()
            print(f"[SCHEDULE] Cron parts: {parts}")

            if len(parts) == 6:
                trigger = CronTrigger(
                    second=parts[0],
                    minute=parts[1],
                    hour=parts[2],
                    day=parts[3],
                    month=parts[4],
                    day_of_week=parts[5],
                    timezone="America/Chicago",
                )
            else:
                trigger = CronTrigger.from_crontab(script.schedule, timezone="America/Chicago")

            scheduler.add_job(
                run_script,
                trigger=trigger,
                args=[script.id],
                id=str(script.id),
                name=script.name,
                replace_existing=True,
            )
            print(f"[SCHEDULE] Successfully added job {script.id} to scheduler")
            print(f"[SCHEDULE] Scheduler now has {len(scheduler.get_jobs())} job(s)")
        except ValueError as e:
            print(f"[SCHEDULE] Failed to schedule script {script.id}: {e}")
            # This will be caught by the endpoint and return a 422
            raise e
    else:
        print(f"[SCHEDULE] Skipping scheduling (enabled={script.enabled}, schedule={script.schedule})")


def run_script(script_id: int):
    """Execute a script with the interpreter for its language, recording a Run row.

    Python scripts get one retry after auto-installing a missing module.
    """
    db = SessionLocal()
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script or not script.enabled:
        db.close()
        return

    run = Run(script_id=script.id)
    db.add(run)
    db.commit()
    db.refresh(run)

    cleanup_path = None
    try:
        resolved = resolve_command(script, run.id)
        cleanup_path = resolved.cleanup_path
        if resolved.error:
            run.exit_code, run.stderr = resolved.error
            run.stdout = ""
            return  # the finally block still records finished_at

        env = os.environ.copy()
        env.update(parse_env_text(script.env))
        if resolved.lang == "python":
            # Let inline Python scripts import helpers that live next to the other scripts.
            env.setdefault("PYTHONPATH", SCRIPTS_DIR)

        for attempt in range(2):  # at most one retry, and only for Python after a pip install
            try:
                result = subprocess.run(
                    resolved.args,
                    shell=resolved.shell,
                    capture_output=True,
                    text=True,
                    cwd=resolved.cwd,
                    env=env,
                    check=False,
                )
            except FileNotFoundError as e:
                run.exit_code = 127
                run.stdout = ""
                run.stderr = f"Interpreter or command not found: {e}"
                break
            except PermissionError as e:
                run.exit_code = 126
                run.stdout = ""
                run.stderr = f"Permission denied: {e}"
                break

            run.exit_code = result.returncode
            run.stdout = result.stdout
            run.stderr = result.stderr

            if result.returncode == 0 or attempt == 1 or resolved.lang != "python":
                break

            module_name = _missing_python_module(result.stderr)
            if not module_name:
                break
            try:
                print(f"Attempting to install missing module: {module_name}")
                subprocess.run(["pip", "install", module_name], capture_output=True, text=True, check=True)
                print(f"Installation of {module_name} successful.")
                # Loop again to retry running the script
            except subprocess.CalledProcessError as install_error:
                print(f"Failed to automatically install module: {install_error}")
                run.stderr = (result.stderr or "") + (
                    f"\n\n[script-runner] Failed to auto-install dependency '{module_name}': "
                    f"{install_error.stderr or install_error}"
                )
                break

    except Exception as e:
        run.exit_code = -1
        run.stderr = str(e)
    finally:
        run.finished_at = datetime.now(CENTRAL_TZ)
        if cleanup_path:
            try:
                os.remove(cleanup_path)
            except OSError:
                pass
        db.commit()
        db.close()


def reschedule_all():
    db = SessionLocal()
    scripts = db.query(Script).all()
    print(f"[STARTUP] Loading {len(scripts)} script(s) from database...")
    for s in scripts:
        if s.schedule:
            print(f"[STARTUP] Scheduling script {s.id}: {s.name} with cron: {s.schedule}")
            try:
                schedule_script(db, s)
                print(f"[STARTUP] Successfully scheduled script {s.id}")
            except Exception as e:
                print(f"[STARTUP] Failed to schedule script {s.id}: {e}")
        else:
            print(f"[STARTUP] Skipping script {s.id}: {s.name} (no schedule)")
    db.close()
    print(f"[STARTUP] Scheduler now has {len(scheduler.get_jobs())} active job(s)")


@app.on_event("startup")
def startup_event():
    # ensure folders
    os.makedirs("/app/data", exist_ok=True)
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    if not USERS:
        print("[AUTH] WARNING: no users configured; set SCRIPT_RUNNER_USERS in .env and run `docker compose up -d`.")
    print("[STARTUP] Running reschedule_all()...")
    reschedule_all()
    print("[STARTUP] Startup complete!")


@app.get("/", response_class=HTMLResponse)
def root_index():
    return (
        "<html><head><title>Script Runner</title></head>"
        "<body><h1>Script Runner backend</h1>"
        "<p>The frontend is served separately. Open <a href=\"http://localhost:3000\">UI</a>.</p>"
        "<p>API root: <a href=\"/api/scripts\">/api/scripts</a></p>"
        "</body></html>"
    )


@app.post("/api/auth/login")
async def login(payload: LoginRequest, response: Response):
    """Start a session. Async so the failure delay is awaited instead of occupying a worker thread."""
    if not _password_ok(payload.username, payload.password):
        await asyncio.sleep(LOGIN_FAIL_DELAY)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _set_session_cookie(response, make_session_token(payload.username))
    return {"username": payload.username}


@app.post("/api/auth/logout")
def logout(response: Response):
    _clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/status")
def auth_status(request: Request):
    username = current_user(request)
    return {"authenticated": username is not None, "username": username, "users_configured": bool(USERS)}


@app.get("/api/languages")
def list_languages():
    """Supported languages and whether their interpreter exists in this backend image."""
    return [
        {
            "id": lang,
            "label": spec["label"],
            "extensions": spec["extensions"],
            "available": interpreter_available(lang),
        }
        for lang, spec in LANGUAGES.items()
    ]


@app.get("/api/scripts", response_model=List[ScriptOut])
def list_scripts():
    db = SessionLocal()
    scripts = db.query(Script).all()
    db.close()
    return scripts


@app.get("/api/scripts/{script_id}", response_model=ScriptOut)
def get_script(script_id: int):
    db = SessionLocal()
    script = db.query(Script).filter(Script.id == script_id).first()
    db.close()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return script


@app.post("/api/scripts", response_model=ScriptOut)
def create_script(script: ScriptCreate):
    db = SessionLocal()
    try:
        db_script = Script(**script.dict())
        db.add(db_script)
        db.commit()
        db.refresh(db_script)
        schedule_script(db, db_script)
        return db_script
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        db.close()


@app.put("/api/scripts/{script_id}", response_model=ScriptOut)
def update_script(script_id: int, payload: ScriptUpdate):
    db = SessionLocal()
    try:
        script = db.query(Script).filter(Script.id == script_id).first()
        if not script:
            raise HTTPException(status_code=404, detail="Script not found")

        for k, v in payload.dict(exclude_unset=True).items():
            setattr(script, k, v)

        # Validate the merged result, not the partial patch.
        try:
            script.command, script.code, script.language = validate_script_fields(
                script.command, script.code, script.language
            )
        except ValueError as e:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(e))

        db.add(script)
        db.commit()
        db.refresh(script)
        try:
            schedule_script(db, script)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return script
    finally:
        db.close()


@app.delete("/api/scripts/{script_id}")
def delete_script(script_id: int):
    db = SessionLocal()
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        db.close()
        raise HTTPException(status_code=404, detail="Script not found")
    try:
        scheduler.remove_job(f"script-{script.id}")
    except Exception:
        pass
    cleanup_inline_files(script.id)
    db.query(Run).filter(Run.script_id == script.id).delete()
    db.delete(script)
    db.commit()
    db.close()
    return {"ok": True}


@app.post("/api/scripts/{script_id}/run")
def run_script_now(script_id: int):
    db = SessionLocal()
    script = db.query(Script).filter(Script.id == script_id).first()
    db.close()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    # run in background thread
    t = threading.Thread(target=run_script, args=(script_id,))
    t.start()
    return {"status": "queued"}


@app.get("/api/scripts/{script_id}/logs")
def get_logs(script_id: int, limit: int = 20):
    db = SessionLocal()
    runs = db.query(Run).filter(Run.script_id == script_id).order_by(Run.started_at.desc()).limit(limit).all()
    db.close()
    return [
        {
            "id": r.id,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "exit_code": r.exit_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }
        for r in runs
    ]


def convert_line_endings(file_path):
    """Converts a file's line endings from CRLF to LF."""
    try:
        with open(file_path, 'rb') as f:
            content = f.read()

        # Only write back if changes are needed
        if b'\r\n' in content:
            content = content.replace(b'\r\n', b'\n')
            with open(file_path, 'wb') as f:
                f.write(content)
    except Exception as e:
        # Log the error but don't block execution
        print(f"Could not convert line endings for {file_path}: {e}")
