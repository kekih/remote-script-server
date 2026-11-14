"""
VixHub-compatible Remote Hosting Server (FastAPI)
Supports: Files, Databases (JSON KV), Scripts, Static Sites, Server self-update.
"""

import os
import sys
import json
import shutil
import signal
import logging
import tempfile
import subprocess
import zipfile
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn

# ============== Config ==============
API_KEY = os.getenv("API_KEY", "change-me-to-a-strong-secret-key-please")
UPDATE_SECRET = os.getenv("UPDATE_SECRET", API_KEY)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DATA_DIR = Path(os.getenv("DATA_DIR", Path.home() / "vixhub_data")).resolve()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
(FILES_ROOT := DATA_DIR / "files").mkdir(exist_ok=True)
(DB_ROOT := DATA_DIR / "databases").mkdir(exist_ok=True)
(SCRIPTS_ROOT := DATA_DIR / "scripts").mkdir(exist_ok=True)
(SITES_ROOT := DATA_DIR / "sites").mkdir(exist_ok=True)
(LOGS_ROOT := DATA_DIR / "logs").mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vixhub-server")

app = FastAPI(title="VixHub Server", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

running_scripts: Dict[str, Dict[str, Any]] = {}


async def verify_api_key(x_api_key: Optional[str] = Depends(api_key_header)):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return x_api_key


def safe_path(base: Path, user_path: str) -> Path:
    target = (base / user_path).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(400, "Invalid path")
    return target


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "2.0.0"
    python: str
    nodejs: Optional[str] = None
    data_dir: str


class DbWriteRequest(BaseModel):
    name: str = "base"
    path: str = ""
    value: Any
    merge: bool = False
    force: bool = False


class ScriptStartRequest(BaseModel):
    id: str
    code: Optional[str] = None


@app.get("/health", response_model=HealthResponse)
async def health():
    py = sys.version.split()[0]
    node = None
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            node = (r.stdout or r.stderr).strip()
    except Exception:
        pass
    return HealthResponse(python=py, nodejs=node, data_dir=str(DATA_DIR))


@app.get("/")
async def root():
    return {"message": "VixHub Server", "docs": "/docs", "health": "/health"}


# ============== FILES ==============
@app.get("/api/v1/files", dependencies=[Depends(verify_api_key)])
async def list_files(path: str = ""):
    target = safe_path(FILES_ROOT, path)
    if not target.exists():
        return {"path": path, "items": []}
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")
    items = []
    for p in sorted(target.iterdir()):
        items.append({
            "name": p.name,
            "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else 0,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
        })
    return {"path": path, "items": items}


@app.post("/api/v1/files/mkdir", dependencies=[Depends(verify_api_key)])
async def mkdir(path: str = Form(...)):
    target = safe_path(FILES_ROOT, path)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": path}


@app.post("/api/v1/files/upload", dependencies=[Depends(verify_api_key)])
async def upload_file(path: str = Form(""), file: UploadFile = File(...)):
    target_dir = safe_path(FILES_ROOT, path)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / (file.filename or "upload.bin")
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "path": str(dest.relative_to(FILES_ROOT))}


@app.get("/api/v1/files/download", dependencies=[Depends(verify_api_key)])
async def download_file(path: str):
    target = safe_path(FILES_ROOT, path)
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name)


@app.delete("/api/v1/files", dependencies=[Depends(verify_api_key)])
async def delete_file(path: str = Query(...)):
    target = safe_path(FILES_ROOT, path)
    if not target.exists():
        raise HTTPException(404, "Not found")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}


# ============== DATABASES ==============
def db_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "base"
    return DB_ROOT / f"{safe}.json"


def load_db(name: str) -> dict:
    p = db_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_db(name: str, data: dict):
    p = db_path(name)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/v1/db", dependencies=[Depends(verify_api_key)])
async def db_list():
    dbs = [p.stem for p in DB_ROOT.glob("*.json")]
    return {"databases": dbs}


@app.get("/api/v1/db/{name}", dependencies=[Depends(verify_api_key)])
async def db_read(name: str, path: str = ""):
    data = load_db(name)
    if not path:
        return {"name": name, "data": data}
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise HTTPException(404, f"Path not found: {path}")
        cur = cur[part]
    return {"name": name, "path": path, "value": cur}


@app.post("/api/v1/db", dependencies=[Depends(verify_api_key)])
async def db_write(req: DbWriteRequest):
    data = load_db(req.name)
    if not req.path:
        if req.force or not data:
            data = req.value if isinstance(req.value, dict) else {"value": req.value}
        elif req.merge and isinstance(req.value, dict):
            data.update(req.value)
        else:
            raise HTTPException(400, "Use force=true or merge=true")
    else:
        parts = req.path.split(".")
        cur = data
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        key = parts[-1]
        if req.merge and key in cur and isinstance(cur[key], dict) and isinstance(req.value, dict):
            cur[key].update(req.value)
        else:
            cur[key] = req.value
    save_db(req.name, data)
    return {"ok": True, "name": req.name}


@app.delete("/api/v1/db/{name}", dependencies=[Depends(verify_api_key)])
async def db_delete(name: str, path: str = ""):
    if not path:
        p = db_path(name)
        if p.exists():
            p.unlink()
        return {"ok": True}
    data = load_db(name)
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        if part not in cur:
            raise HTTPException(404, "Path not found")
        cur = cur[part]
    if parts[-1] in cur:
        del cur[parts[-1]]
        save_db(name, data)
    return {"ok": True}


# ============== SCRIPTS ==============
@app.get("/api/v1/scripts", dependencies=[Depends(verify_api_key)])
async def list_scripts():
    items = []
    for p in sorted(SCRIPTS_ROOT.glob("*.py")):
        sid = p.stem
        running = sid in running_scripts and running_scripts[sid]["process"].poll() is None
        items.append({
            "id": sid,
            "running": running,
            "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
        })
    return {"scripts": items}


@app.post("/api/v1/scripts/upload", dependencies=[Depends(verify_api_key)])
async def upload_script(id: str = Form(...), file: UploadFile = File(...)):
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_") or "script"
    dest = SCRIPTS_ROOT / f"{safe_id}.py"
    content = await file.read()
    dest.write_bytes(content)
    return {"ok": True, "id": safe_id}


@app.post("/api/v1/scripts/start", dependencies=[Depends(verify_api_key)])
async def start_script(req: ScriptStartRequest):
    safe_id = "".join(c for c in req.id if c.isalnum() or c in "-_") or "script"
    script_file = SCRIPTS_ROOT / f"{safe_id}.py"
    if req.code is not None:
        script_file.write_text(req.code, encoding="utf-8")
    if not script_file.exists():
        raise HTTPException(404, "Script not found. Upload first.")
    if safe_id in running_scripts:
        proc = running_scripts[safe_id]["process"]
        if proc.poll() is None:
            raise HTTPException(400, "Already running")
    log_file = LOGS_ROOT / f"{safe_id}.log"
    log_f = open(log_file, "a", encoding="utf-8")
    log_f.write(f"\n=== START {datetime.now().isoformat()} ===\n")
    log_f.flush()
    proc = subprocess.Popen(
        [sys.executable, str(script_file)],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=str(SCRIPTS_ROOT),
        start_new_session=True,
    )
    running_scripts[safe_id] = {
        "process": proc,
        "log_file": log_file,
        "log_handle": log_f,
        "started": datetime.now().isoformat(),
    }
    return {"ok": True, "id": safe_id, "pid": proc.pid}


@app.post("/api/v1/scripts/stop", dependencies=[Depends(verify_api_key)])
async def stop_script(id: str = Form(...)):
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_")
    if safe_id not in running_scripts:
        return {"ok": True, "message": "Not running"}
    info = running_scripts[safe_id]
    proc = info["process"]
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    try:
        info["log_handle"].close()
    except Exception:
        pass
    del running_scripts[safe_id]
    return {"ok": True}


@app.get("/api/v1/scripts/logs", dependencies=[Depends(verify_api_key)])
async def script_logs(id: str, tail: int = 200):
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_")
    log_file = LOGS_ROOT / f"{safe_id}.log"
    if not log_file.exists():
        return {"id": safe_id, "logs": ""}
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"id": safe_id, "logs": "\n".join(lines[-tail:])}


@app.delete("/api/v1/scripts/{id}", dependencies=[Depends(verify_api_key)])
async def delete_script(id: str):
    await stop_script(id)
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_")
    p = SCRIPTS_ROOT / f"{safe_id}.py"
    if p.exists():
        p.unlink()
    return {"ok": True}


# ============== SITES ==============
@app.get("/api/v1/sites", dependencies=[Depends(verify_api_key)])
async def list_sites():
    sites = []
    for p in sorted(SITES_ROOT.iterdir()):
        if p.is_dir():
            has_index = (p / "index.html").exists()
            sites.append({"id": p.name, "has_index": has_index})
    return {"sites": sites}


@app.post("/api/v1/sites", dependencies=[Depends(verify_api_key)])
async def create_site(
    id: str = Form(...),
    update: bool = Form(False),
    file: UploadFile = File(None),
):
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_") or "site"
    site_dir = SITES_ROOT / safe_id
    if site_dir.exists() and not update:
        raise HTTPException(400, "Site exists. Use update=true")
    site_dir.mkdir(parents=True, exist_ok=True)
    if update:
        for child in list(site_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    if file and file.filename:
        if file.filename.lower().endswith(".zip"):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp.write(await file.read())
            tmp.close()
            with zipfile.ZipFile(tmp.name, "r") as z:
                z.extractall(site_dir)
            os.unlink(tmp.name)
        else:
            dest = site_dir / file.filename
            dest.write_bytes(await file.read())
    return {"ok": True, "id": safe_id, "url": f"/sites/{safe_id}/"}


@app.delete("/api/v1/sites/{id}", dependencies=[Depends(verify_api_key)])
async def delete_site(id: str):
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_")
    site_dir = SITES_ROOT / safe_id
    if site_dir.exists():
        shutil.rmtree(site_dir)
    return {"ok": True}


app.mount("/sites", StaticFiles(directory=str(SITES_ROOT), html=True), name="sites")


# ============== SERVER UPDATE ==============
@app.post("/api/v1/update_server")
async def update_server(
    secret: str = Query(...),
    authorization: Optional[str] = Header(None),
    file: UploadFile = File(...),
):
    token_ok = False
    if authorization and authorization.startswith("Bearer "):
        token_ok = authorization[7:] == UPDATE_SECRET
    if secret != UPDATE_SECRET and not token_ok:
        raise HTTPException(401, "Invalid secret")
    content = await file.read()
    try:
        compile(content, "server.py", "exec")
    except SyntaxError as e:
        raise HTTPException(400, f"Syntax error: {e}")
    server_path = Path(__file__).resolve()
    bak = server_path.with_suffix(".py.bak")
    shutil.copy2(server_path, bak)
    server_path.write_bytes(content)
    logger.warning("Server file updated, restarting in 1.5s...")
    def _restart():
        import time
        time.sleep(1.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    import threading
    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True, "message": "Updated, restarting..."}


# ============== Legacy execute ==============
class ExecuteRequest(BaseModel):
    language: str = "python"
    code: str
    timeout: int = 30


@app.post("/api/v1/execute", dependencies=[Depends(verify_api_key)])
async def execute(req: ExecuteRequest):
    if req.language not in ("python", "nodejs"):
        raise HTTPException(400, "language must be python or nodejs")
    suffix = ".py" if req.language == "python" else ".js"
    interp = sys.executable if req.language == "python" else "node"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / f"s{suffix}"
        script.write_text(req.code, encoding="utf-8")
        try:
            r = subprocess.run(
                [interp, str(script)],
                capture_output=True,
                text=True,
                timeout=min(req.timeout, 120),
                cwd=tmp,
            )
            return {
                "language": req.language,
                "stdout": r.stdout or "",
                "stderr": r.stderr or "",
                "exit_code": r.returncode,
                "timed_out": False,
                "error": None,
            }
        except subprocess.TimeoutExpired:
            return {
                "language": req.language,
                "stdout": "",
                "stderr": "Timeout",
                "exit_code": -9,
                "timed_out": True,
                "error": None,
            }


if __name__ == "__main__":
    if API_KEY == "change-me-to-a-strong-secret-key-please":
        logger.warning("Using default API_KEY! Set a strong secret.")
    logger.info(f"Data dir: {DATA_DIR}")
    logger.info(f"Starting on {HOST}:{PORT}")
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, log_level="info")
