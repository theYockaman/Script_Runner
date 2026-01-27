import os
import shlex
import subprocess
import threading
from datetime import datetime
from typing import List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String,
                        Text, create_engine, event)
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from fastapi.responses import HTMLResponse

# Config
DB_PATH = os.environ.get("DATABASE_URL", "sqlite:///app/data/data.db")
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/app/scripts")

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
    command = Column(Text, nullable=False)
    schedule = Column(String(100), nullable=True)
    enabled = Column(Boolean, default=True)
    env = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    exit_code = Column(Integer, nullable=True)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)


Base.metadata.create_all(bind=engine)

app = FastAPI(title="Script Runner")
scheduler = BackgroundScheduler()
scheduler.start()


class ScriptCreate(BaseModel):
    name: str
    command: str
    schedule: Optional[str] = None
    enabled: Optional[bool] = True
    env: Optional[str] = None


class ScriptUpdate(BaseModel):
    name: Optional[str]
    command: Optional[str]
    schedule: Optional[str]
    enabled: Optional[bool]
    env: Optional[str]


class ScriptOut(BaseModel):
    id: int
    name: str
    command: str
    schedule: Optional[str]
    enabled: bool
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
    # always remove before adding
    if scheduler.get_job(str(script.id)):
        scheduler.remove_job(str(script.id))

    if script.enabled and script.schedule:
        try:
            # Support both 5-part and 6-part cron expressions
            parts = script.schedule.split()
            if len(parts) == 6:
                trigger = CronTrigger(
                    second=parts[0],
                    minute=parts[1],
                    hour=parts[2],
                    day=parts[3],
                    month=parts[4],
                    day_of_week=parts[5],
                    timezone="UTC",
                )
            else:
                trigger = CronTrigger.from_crontab(script.schedule, timezone="UTC")

            scheduler.add_job(
                run_script,
                trigger=trigger,
                args=[script.id],
                id=str(script.id),
                name=script.name,
                replace_existing=True,
            )
        except ValueError as e:
            print(f"Failed to schedule script {script.id}: {e}")
            # This will be caught by the endpoint and return a 422
            raise e


def run_script(script_id: int):
    """Execute a script command."""
    db = SessionLocal()
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script or not script.enabled:
        db.close()
        return

    run = Run(script_id=script.id)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        command_to_run = script.command
        if command_to_run.endswith(".py"):
            command_to_run = f"python -u {command_to_run}"

        # The command is executed with shell=True, so we can pass the command string directly.
        # This allows running shell scripts, python scripts with a shebang, or any other command.
        result = subprocess.run(
            command_to_run,
            shell=True,
            capture_output=True,
            text=True,
            cwd=SCRIPTS_DIR,
            check=False,
        )
        run.exit_code = result.returncode
        run.stdout = result.stdout
        run.stderr = result.stderr
    except Exception as e:
        run.exit_code = -1
        run.stderr = str(e)
    finally:
        run.finished_at = datetime.utcnow()
        db.commit()
        db.close()


def reschedule_all():
    db = SessionLocal()
    scripts = db.query(Script).all()
    for s in scripts:
        schedule_script(db, s)
    db.close()


@app.on_event("startup")
def startup_event():
    # ensure folders
    os.makedirs("/app/data", exist_ok=True)
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    reschedule_all()


@app.get("/", response_class=HTMLResponse)
def root_index():
    return (
        "<html><head><title>Script Runner</title></head>"
        "<body><h1>Script Runner backend</h1>"
        "<p>The frontend is served separately. Open <a href=\"http://localhost:3000\">UI</a>.</p>"
        "<p>API root: <a href=\"/api/scripts\">/api/scripts</a></p>"
        "</body></html>"
    )


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
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        db.close()
        raise HTTPException(status_code=404, detail="Script not found")

    for k, v in payload.dict(exclude_unset=True).items():
        setattr(script, k, v)

    db.add(script)
    db.commit()
    db.refresh(script)
    schedule_script(db, script)
    db.close()
    return script


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
