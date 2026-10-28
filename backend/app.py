"""Single-host execution service. Start exactly one API worker."""
import asyncio
import codecs
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("MISSION_DATA", str(ROOT / "data"))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "mission.sqlite3"
TZ = ZoneInfo("Asia/Taipei")
MAX_ACTIVE = max(1, int(os.getenv("MISSION_CONCURRENCY", "2")))
MAX_BYTES = max(1024, int(os.getenv("MISSION_LOG_BYTES", "1048576")))
RETENTION_DAYS = max(1, int(os.getenv("MISSION_RETENTION_DAYS", "30")))
running = {}
pending = set()
runner_tasks = set()
stop_event = asyncio.Event()

def now():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.isoformat() if dt else None

def conn():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c

def initialize():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            cwd TEXT, args TEXT NOT NULL, env TEXT NOT NULL,
            timeout INTEGER NOT NULL, retries INTEGER NOT NULL,
            enabled INTEGER NOT NULL, schedule_kind TEXT NOT NULL, schedule_value TEXT,
            next_at TEXT, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_name TEXT NOT NULL,
            state TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
            exit_code INTEGER, reason TEXT, attempt INTEGER NOT NULL DEFAULT 0,
            log_path TEXT NOT NULL)""")
        c.execute("UPDATE runs SET state='interrupted', reason='Server restarted while running', ended_at=? WHERE state IN ('running','queued')", (iso(now()),))

class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    target: str = Field(min_length=1)
    cwd: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = Field(default=300, ge=1, le=86400)
    retries: int = Field(default=0, ge=0, le=5)
    enabled: bool = True
    schedule_kind: str = "none"
    schedule_value: str | None = None

    @field_validator("kind")
    @classmethod
    def kind_check(cls, v):
        if v not in ("python", "powershell", "bat", "http"):
            raise ValueError("Unsupported task type")
        return v

    @field_validator("schedule_kind")
    @classmethod
    def schedule_check(cls, v):
        if v not in ("none", "once", "interval", "cron"):
            raise ValueError("Unsupported schedule")
        return v

def calculate_next(kind, value, anchor=None):
    anchor = anchor or now()
    if kind == "none":
        return None
    if kind == "once":
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(timezone.utc) if dt.astimezone(timezone.utc) > anchor else None
    if kind == "interval":
        secs = int(value)
        if secs < 10:
            raise ValueError("Interval must be at least 10 seconds")
        return anchor + timedelta(seconds=secs)
    if kind == "cron":
        return croniter(value, anchor.astimezone(TZ)).get_next(datetime).astimezone(timezone.utc)
    raise ValueError("Unsupported schedule")

def validate_task(t):
    if t.kind != "http":
        p = Path(t.target).expanduser()
        if not p.is_file():
            raise ValueError("Script file does not exist")
        if p.suffix.lower() not in {"python": (".py",), "powershell": (".ps1",), "bat": (".bat", ".cmd")}[t.kind]:
            raise ValueError("Script extension does not match task type")
    else:
        from urllib.parse import urlsplit
        u = urlsplit(t.target)
        if u.scheme not in ("http", "https") or not u.netloc or u.username or u.password:
            raise ValueError("Valid HTTP(S) URL required")
    if t.cwd and not Path(t.cwd).is_dir():
        raise ValueError("Working directory does not exist")
    if any("\x00" in x for x in [t.target, *t.args, *t.env.keys(), *t.env.values()]):
        raise ValueError("Null bytes not allowed")
    try:
        calculate_next(t.schedule_kind, t.schedule_value)
    except Exception as exc:
        raise ValueError("Invalid schedule: " + str(exc)) from exc

def task_out(row):
    d = dict(row)
    d["args"] = json.loads(d["args"])
    d["env_keys"] = list(json.loads(d.pop("env")).keys())
    d["enabled"] = bool(d["enabled"])
    return d

def read_task(task_id):
    with conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Task not found")
    return r

def log_file(run_id):
    return DATA / "logs" / (run_id + ".log")

def append_log(path, msg):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        if f.tell() < MAX_BYTES:
            f.write(msg[:max(0, MAX_BYTES - f.tell())])

def persist_run(run_id, **changes):
    if not changes:
        return
    with conn() as c:
        c.execute("UPDATE runs SET " + ", ".join(k + "=?" for k in changes) + " WHERE id=?",
                  (*changes.values(), run_id))

async def kill_tree(proc):
    if proc.returncode is not None:
        return
    if os.name == "nt":
        await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                                              stdout=asyncio.subprocess.DEVNULL,
                                              stderr=asyncio.subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()

async def execute(run_id, row):
    task_id = row["id"]
    path = log_file(run_id)
    running[run_id] = {"task_id": task_id, "process": None, "cancel": False}
    persist_run(run_id, state="running")
    attempt = 0
    final = ("failed", None, "Unknown error")
    try:
        for attempt in range(row["retries"] + 1):
            persist_run(run_id, attempt=attempt + 1)
            append_log(path, f"--- Attempt {attempt + 1} ---\n")
            if running[run_id]["cancel"]:
                final = ("cancelled", None, "Cancelled by user")
                break
            try:
                if row["kind"] == "http":
                    async with httpx.AsyncClient(timeout=row["timeout"], follow_redirects=False, trust_env=False) as client:
                        response = await client.get(row["target"], headers=json.loads(row["env"]))
                        append_log(path, f"HTTP {response.status_code}\n" + response.text[:MAX_BYTES] + "\n")
                        rc = 0 if 200 <= response.status_code < 300 else response.status_code
                else:
                    if row["kind"] == "python":
                        argv = [sys.executable, "-u", row["target"]]
                    elif row["kind"] == "powershell":
                        exe = shutil.which("pwsh") or shutil.which("powershell")
                        if not exe:
                            raise RuntimeError("PowerShell executable not found")
                        argv = [exe, "-NoProfile", "-NonInteractive", "-File", row["target"]]
                    else:
                        if os.name != "nt":
                            raise RuntimeError("BAT requires Windows host")
                        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", row["target"]]
                    argv += json.loads(row["args"])
                    env = os.environ.copy()
                    env.update(json.loads(row["env"]))
                    env["PYTHONIOENCODING"] = "utf-8"
                    proc = await asyncio.create_subprocess_exec(
                        *argv, cwd=row["cwd"] or str(Path(row["target"]).parent),
                        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                        start_new_session=os.name != "nt")
                    running[run_id]["process"] = proc
                    async def pump(stream, label):
                        decoder = codecs.getincrementaldecoder("utf-8")("replace")
                        while chunk := await stream.read(2048):
                            append_log(path, label + decoder.decode(chunk))
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            append_log(path, label + tail)
                    pipes = [asyncio.create_task(pump(proc.stdout, "")),
                             asyncio.create_task(pump(proc.stderr, "[stderr] "))]
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=row["timeout"])
                    except asyncio.TimeoutError:
                        await kill_tree(proc)
                        final = ("timeout", None, "Timeout exceeded")
                        await asyncio.gather(*pipes)
                        break
                    finally:
                        await asyncio.gather(*pipes)
                    rc = proc.returncode
                    running[run_id]["process"] = None
                if running[run_id]["cancel"]:
                    final = ("cancelled", rc, "Cancelled by user")
                    break
                if rc == 0:
                    final = ("success", rc, None)
                    break
                final = ("failed", rc, f"Exit code / HTTP status: {rc}")
            except asyncio.CancelledError:
                final = ("interrupted", None, "Server shutdown")
                proc = running[run_id]["process"]
                if proc:
                    await kill_tree(proc)
                break
            except Exception as exc:
                final = ("failed", None, f"{type(exc).__name__}: {exc}")
                append_log(path, final[2] + "\n")
            if attempt < row["retries"]:
                await asyncio.sleep(min(2 ** attempt, 30))
    finally:
        persist_run(run_id, state=final[0], exit_code=final[1], reason=final[2],
                    ended_at=iso(now()))
        running.pop(run_id, None)

def start_run(row):
    tid = row["id"]
    if tid in pending or any(x["task_id"] == tid for x in running.values()):
        raise HTTPException(409, "Task already running or queued")
    if len(pending) + len(running) >= MAX_ACTIVE:
        raise HTTPException(429, "Global concurrency limit reached")
    run_id = str(uuid.uuid4())
    pending.add(tid)
    with conn() as c:
        c.execute("INSERT INTO runs(id,task_id,task_name,state,started_at,log_path) VALUES(?,?,?,?,?,?)",
                  (run_id, tid, row["name"], "queued", iso(now()), str(log_file(run_id))))
    async def wrapper():
        try:
            await execute(run_id, row)
        finally:
            pending.discard(tid)
    t = asyncio.create_task(wrapper())
    runner_tasks.add(t)
    t.add_done_callback(runner_tasks.discard)
    return run_id

async def scheduler():
    while not stop_event.is_set():
        at = now()
        with conn() as c:
            due = c.execute("SELECT * FROM tasks WHERE enabled=1 AND next_at IS NOT NULL AND next_at<=?",
                            (iso(at),)).fetchall()
            for row in due:
                # Misfires: at most one run on restart; skip missed interval ticks.
                try:
                    nxt = calculate_next(row["schedule_kind"], row["schedule_value"], at)
                except Exception:
                    nxt = None
                if row["schedule_kind"] == "once":
                    c.execute("UPDATE tasks SET next_at=NULL, enabled=0 WHERE id=?", (row["id"],))
                else:
                    c.execute("UPDATE tasks SET next_at=? WHERE id=?", (iso(nxt), row["id"]))
        for row in due:
            try:
                start_run(row)
            except HTTPException:
                pass
        cutoff = iso(at - timedelta(days=RETENTION_DAYS))
        with conn() as c:
            old = c.execute("SELECT id,log_path FROM runs WHERE ended_at IS NOT NULL AND ended_at<?",
                            (cutoff,)).fetchall()
            c.execute("DELETE FROM runs WHERE ended_at IS NOT NULL AND ended_at<?", (cutoff,))
        for r in old:
            Path(r["log_path"]).unlink(missing_ok=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass

@asynccontextmanager
async def lifespan(app):
    initialize()
    stop_event.clear()
    worker = asyncio.create_task(scheduler())
    yield
    stop_event.set()
    await worker
    for state in list(running.values()):
        if state["process"]:
            await kill_tree(state["process"])
    for task in list(runner_tasks):
        task.cancel()
    if runner_tasks:
        await asyncio.gather(*runner_tasks, return_exceptions=True)

app = FastAPI(title="Big Mission", lifespan=lifespan)

@app.get("/api/tasks")
def tasks():
    with conn() as c:
        return [task_out(r) for r in c.execute("SELECT * FROM tasks ORDER BY created_at DESC")]

@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskIn):
    try:
        validate_task(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    task_id = str(uuid.uuid4())
    fields = payload.model_dump()
    fields.update(id=task_id, created_at=iso(now()),
                  next_at=iso(calculate_next(payload.schedule_kind, payload.schedule_value)),
                  args=json.dumps(payload.args), env=json.dumps(payload.env))
    with conn() as c:
        c.execute("INSERT INTO tasks(" + ",".join(fields) + ") VALUES(" +
                  ",".join("?" for _ in fields) + ")", tuple(fields.values()))
    return task_out(read_task(task_id))

@app.put("/api/tasks/{task_id}")
def edit_task(task_id: str, payload: TaskIn):
    read_task(task_id)
    try:
        validate_task(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    fields = payload.model_dump()
    fields.update(next_at=iso(calculate_next(payload.schedule_kind, payload.schedule_value)),
                  args=json.dumps(payload.args), env=json.dumps(payload.env))
    with conn() as c:
        c.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                  (*fields.values(), task_id))
    return task_out(read_task(task_id))

@app.post("/api/tasks/{task_id}/copy", status_code=201)
def copy_task(task_id: str):
    row = read_task(task_id)
    values = dict(row)
    values.update(id=str(uuid.uuid4()), name=row["name"] + " (副本)", enabled=0,
                  next_at=None, created_at=iso(now()))
    with conn() as c:
        c.execute("INSERT INTO tasks(" + ",".join(values) + ") VALUES(" +
                  ",".join("?" for _ in values) + ")", tuple(values.values()))
    return task_out(read_task(values["id"]))

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    read_task(task_id)
    if task_id in pending or any(v["task_id"] == task_id for v in running.values()):
        raise HTTPException(409, "Cannot delete active task")
    with conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"deleted": True}

@app.post("/api/tasks/{task_id}/run", status_code=202)
def run_task(task_id: str):
    return {"run_id": start_run(read_task(task_id))}

@app.get("/api/runs")
def runs(q: str = "", state: str = ""):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,task_id,task_name,state,started_at,ended_at,exit_code,reason,attempt FROM runs "
            "WHERE (task_name LIKE ? OR id LIKE ?) AND (?='' OR state=?) ORDER BY started_at DESC LIMIT 500",
            (f"%{q}%", f"%{q}%", state, state))]

@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    with conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    return dict(row)

@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: str):
    state = running.get(run_id)
    if not state:
        raise HTTPException(409, "Run is not active")
    state["cancel"] = True
    if state["process"]:
        await kill_tree(state["process"])
    return {"cancelling": True}

@app.get("/api/runs/{run_id}/log")
def get_log(run_id: str):
    run_detail(run_id)
    path = log_file(run_id)
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=run_id + ".log") if path.exists() else {"message": "No output yet"}

@app.get("/api/runs/{run_id}/stream")
async def stream_log(run_id: str):
    run_detail(run_id)
    async def events():
        pos = 0
        while True:
            path = log_file(run_id)
            if path.exists():
                with path.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                if chunk:
                    yield "data: " + json.dumps({"text": chunk.decode("utf-8", "replace")}) + "\n\n"
            status = run_detail(run_id)["state"]
            if status not in ("running", "queued"):
                yield "event: done\ndata: " + json.dumps({"state": status}) + "\n\n"
                break
            await asyncio.sleep(0.35)
    return StreamingResponse(events(), media_type="text/event-stream")

@app.get("/api/dashboard")
def dashboard():
    with conn() as c:
        states = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, count(*) n FROM runs GROUP BY state")}
        upcoming = [dict(r) for r in c.execute(
            "SELECT id,name,next_at FROM tasks WHERE enabled=1 AND next_at IS NOT NULL ORDER BY next_at LIMIT 10")]
    return {"states": states, "upcoming": upcoming, "concurrency_limit": MAX_ACTIVE}

@app.get("/api/health")
def health():
    return {"ok": True, "time": iso(now())}

if (ROOT / "frontend" / "dist").exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(ROOT / "frontend" / "dist"), html=True), name="ui")
