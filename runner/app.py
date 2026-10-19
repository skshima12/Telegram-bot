import os, re, uuid, sqlite3, secrets, logging, shutil
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form, Query, Body
from fastapi.responses import JSONResponse
import docker
from docker.errors import DockerException, NotFound, APIError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sk-runner")
SECRET = os.getenv("RUNNER_API_SECRET", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
DB_PATH = DATA_DIR / "runner.sqlite3"
MAX_UPLOAD = 8 * 1024 * 1024
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

if not SECRET:
    raise RuntimeError("RUNNER_API_SECRET is required")

app = FastAPI(title="SK Bot Hosting Runner", docs_url=None, redoc_url=None)

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL,
        language TEXT NOT NULL, entrypoint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'created',
        container_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    con.commit()
    return con

def auth(authorization: Optional[str]):
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {SECRET}"):
        raise HTTPException(status_code=401, detail="Unauthorized")

def docker_client():
    try:
        return docker.from_env()
    except DockerException:
        raise HTTPException(status_code=503, detail="Docker daemon unavailable on runner host")

def owned(project_id: str, owner_id: int):
    with connect() as con:
        row = con.execute("SELECT * FROM projects WHERE id=? AND owner_id=?", (project_id, owner_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)

def safe_filename(name: str):
    name = Path(name or "").name
    if not name or name in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return name

def save_upload(file: UploadFile, target: Path):
    name = safe_filename(file.filename or "")
    suffix = Path(name).suffix.lower()
    if name not in {"requirements.txt", "package.json"} and suffix not in {".py", ".js"}:
        raise HTTPException(status_code=400, detail="Only .py, .js, requirements.txt and package.json are allowed")
    target.mkdir(parents=True, exist_ok=True)
    dest = target / name
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = file.file.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File exceeds 8 MB limit")
            out.write(chunk)
    return name

def validate_manifest(project_dir: Path, language: str):
    req = project_dir / "requirements.txt"
    pkg = project_dir / "package.json"
    if language == "python" and pkg.exists():
        raise HTTPException(status_code=400, detail="Python projects cannot include package.json")
    if language == "javascript" and req.exists():
        raise HTTPException(status_code=400, detail="JavaScript projects cannot include requirements.txt")
    if pkg.exists():
        import json
        try:
            manifest = json.loads(pkg.read_text("utf-8"))
            deps = manifest.get("dependencies", {})
            if not isinstance(deps, dict) or len(deps) > 100:
                raise ValueError("invalid dependencies")
            # npm is invoked with --ignore-scripts, so package lifecycle scripts are not run.
            # Workspaces are excluded because they can reference files outside this single-project upload.
            if "workspaces" in manifest:
                raise ValueError("workspaces are not supported in this starter")
        except Exception:
            raise HTTPException(status_code=400, detail="package.json invalid or contains unsupported scripts/workspaces")
    if req.exists() and req.stat().st_size > 64 * 1024:
        raise HTTPException(status_code=400, detail="requirements.txt too large")

def get_container(project):
    cid = project.get("container_id")
    if not cid:
        return None
    client = docker_client()
    try:
        return client.containers.get(cid)
    except NotFound:
        return None

def container_status(project):
    container = get_container(project)
    if container is None:
        return "stopped" if project["status"] in {"running", "stopping"} else project["status"]
    try:
        container.reload()
        return "running" if container.status == "running" else container.status
    except Exception:
        return project["status"]

def create_container(project):
    client = docker_client()
    pdir = PROJECTS_DIR / str(project["owner_id"]) / project["id"]
    validate_manifest(pdir, project["language"])
    entry = project["entrypoint"]
    if project["language"] == "python":
        image = "python:3.12-alpine"
        command = ["sh", "-lc", "mkdir -p /tmp/app && cp -R /app/. /tmp/app/ && cd /tmp/app && "
                   "if [ -f requirements.txt ]; then pip install --disable-pip-version-check --no-cache-dir --target /tmp/pylibs -r requirements.txt; fi && "
                   "PYTHONPATH=/tmp/pylibs${PYTHONPATH:+:$PYTHONPATH} python -u " + entry]
    else:
        image = "node:22-alpine"
        command = ["sh", "-lc", "mkdir -p /tmp/app && cp -R /app/. /tmp/app/ && cd /tmp/app && "
                   "if [ -f package.json ]; then npm install --omit=dev --ignore-scripts --no-audit --no-fund; fi && node " + entry]
    try:
        container = client.containers.run(
            image=image,
            command=command,
            detach=True,
            name=f"skhost-{project['id']}",
            user="10001:10001",
            working_dir="/tmp",
            volumes={str(pdir): {"bind": "/app", "mode": "ro"}},
            network_mode="bridge",
            mem_limit="256m",
            nano_cpus=500_000_000,
            pids_limit=64,
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=160m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            init=True,
            restart_policy={"Name": "no"},
            labels={"managed-by": "sk-bot-hosting", "owner-id": str(project["owner_id"]), "project-id": project["id"]},
        )
        return container
    except APIError as exc:
        log.exception("container create failed")
        raise HTTPException(status_code=500, detail=f"Could not start container: {str(exc)[:180]}")

@app.get("/health")
def health():
    return {"ok": True, "service": "sk-hosting-runner"}

@app.post("/projects")
async def create_project(authorization: Optional[str] = Header(None), owner_id: int = Form(...),
                         name: str = Form(...), file: UploadFile = File(...)):
    auth(authorization)
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise HTTPException(status_code=400, detail="Invalid project name")
    filename = safe_filename(file.filename or "")
    ext = Path(filename).suffix.lower()
    if ext not in {".py", ".js"}:
        raise HTTPException(status_code=400, detail="Main file must be .py or .js")
    with connect() as con:
        count = con.execute("SELECT COUNT(*) FROM projects WHERE owner_id=?", (owner_id,)).fetchone()[0]
    if count >= 5:
        raise HTTPException(status_code=429, detail="Starter limit: maximum 5 projects per user")
    pid = uuid.uuid4().hex[:12]
    language = "python" if ext == ".py" else "javascript"
    pdir = PROJECTS_DIR / str(owner_id) / pid
    saved = save_upload(file, pdir)
    with connect() as con:
        con.execute("INSERT INTO projects(id,owner_id,name,language,entrypoint,status) VALUES(?,?,?,?,?,?)",
                    (pid, owner_id, name, language, saved, "created"))
    return {"id": pid, "name": name, "language": language, "status": "created"}

@app.post("/projects/{pid}/files")
async def add_file(pid: str, authorization: Optional[str] = Header(None), owner_id: int = Form(...),
                   file: UploadFile = File(...)):
    auth(authorization)
    project = owned(pid, owner_id)
    if project["status"] == "running":
        raise HTTPException(status_code=409, detail="Stop project before changing dependencies")
    pdir = PROJECTS_DIR / str(owner_id) / pid
    name = safe_filename(file.filename or "")
    expected = "requirements.txt" if project["language"] == "python" else "package.json"
    if name != expected:
        raise HTTPException(status_code=400, detail=f"Expected {expected}")
    save_upload(file, pdir)
    validate_manifest(pdir, project["language"])
    return {"ok": True, "filename": name}

@app.get("/projects")
def list_projects(authorization: Optional[str] = Header(None), owner_id: int = Query(...)):
    auth(authorization)
    with connect() as con:
        rows = con.execute("SELECT * FROM projects WHERE owner_id=? ORDER BY created_at DESC", (owner_id,)).fetchall()
    items = []
    for row in rows:
        p = dict(row)
        p["status"] = container_status(p)
        items.append({k: p[k] for k in ("id", "name", "language", "status", "created_at")})
    return {"projects": items}

@app.post("/projects/{pid}/deploy")
def deploy(pid: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    auth(authorization)
    owner_id = int(body.get("owner_id", 0))
    p = owned(pid, owner_id)
    old = get_container(p)
    if old:
        try:
            old.reload()
            if old.status == "running":
                return {"status": "running"}
            old.remove(force=True)
        except Exception:
            pass
    container = create_container(p)
    with connect() as con:
        con.execute("UPDATE projects SET container_id=?,status='running' WHERE id=?", (container.id, pid))
    return {"status": "running"}

@app.post("/projects/{pid}/stop")
def stop(pid: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    auth(authorization)
    p = owned(pid, int(body.get("owner_id", 0)))
    c = get_container(p)
    if c:
        try:
            c.stop(timeout=5)
        except Exception:
            pass
    with connect() as con:
        con.execute("UPDATE projects SET status='stopped' WHERE id=?", (pid,))
    return {"status": "stopped"}

@app.post("/projects/{pid}/restart")
def restart(pid: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    auth(authorization)
    p = owned(pid, int(body.get("owner_id", 0)))
    c = get_container(p)
    if c:
        try:
            c.remove(force=True)
        except Exception:
            pass
    container = create_container(p)
    with connect() as con:
        con.execute("UPDATE projects SET container_id=?,status='running' WHERE id=?", (container.id, pid))
    return {"status": "running"}

@app.get("/projects/{pid}/logs")
def logs(pid: str, authorization: Optional[str] = Header(None), owner_id: int = Query(...)):
    auth(authorization)
    p = owned(pid, owner_id)
    c = get_container(p)
    if not c:
        return {"logs": "(Project is not running; no container logs available.)"}
    try:
        output = c.logs(tail=100, timestamps=True).decode("utf-8", "replace")
        return {"logs": output[-12000:]}
    except Exception as exc:
        return {"logs": f"Unable to read logs: {str(exc)[:200]}"}

@app.delete("/projects/{pid}")
def delete_project(pid: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    auth(authorization)
    p = owned(pid, int(body.get("owner_id", 0)))
    c = get_container(p)
    if c:
        try:
            c.remove(force=True)
        except Exception:
            pass
    with connect() as con:
        con.execute("DELETE FROM projects WHERE id=?", (pid,))
    shutil.rmtree(PROJECTS_DIR / str(p["owner_id"]) / pid, ignore_errors=True)
    return {"status": "deleted"}
