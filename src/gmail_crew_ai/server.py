from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from gmail_crew_ai.crew import GmailCrewAi


@dataclass
class RunRecord:
    id: str
    email_address: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    status: str = "running"  # running|completed|failed
    return_code: Optional[int] = None
    logs: List[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append_log(self, line: str) -> None:
        with self._lock:
            # Avoid huge memory growth by trimming oldest lines if extremely long
            self.logs.append(line.rstrip("\n"))
            if len(self.logs) > 50000:
                del self.logs[:10000]


class TeeStream(io.TextIOBase):
    def __init__(self, original, write_callback):
        self.original = original
        self.write_callback = write_callback
        self._buffer = ""

    def write(self, s):
        # Write-through to original stream
        try:
            self.original.write(s)
        except Exception:
            pass
        # Buffer and emit complete lines to callback
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.write_callback(line)
        return len(s)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass


class RunManager:
    def __init__(self):
        self.runs: Dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def create_run(self, email_address: str) -> RunRecord:
        run_id = str(uuid.uuid4())
        record = RunRecord(id=run_id, email_address=email_address)
        with self._lock:
            self.runs[run_id] = record
        return record

    def get(self, run_id: str) -> RunRecord:
        rec = self.runs.get(run_id)
        if not rec:
            raise KeyError(run_id)
        return rec

    def list(self) -> List[RunRecord]:
        with self._lock:
            return list(self.runs.values())


run_manager = RunManager()
app = FastAPI(title="Gmail Crew AI Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_crew_thread(record: RunRecord, app_password: str, email_limit: int) -> None:
    # Set environment variables for this thread execution
    # Note: This modifies process-wide env; ensure only one run at a time if that matters.
    os.environ["EMAIL_ADDRESS"] = record.email_address
    os.environ["APP_PASSWORD"] = app_password

    # Capture stdout/stderr
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    def emit(line: str):
        # Simple redaction of the email and app password if they appear in logs
        redacted = line.replace(app_password, "********").replace(record.email_address, _mask_email(record.email_address))
        record.append_log(redacted)

    sys.stdout = TeeStream(original_stdout, emit)
    sys.stderr = TeeStream(original_stderr, emit)

    try:
        emit(f"[server] Starting crew run at {datetime.utcnow().isoformat()}Z with limit={email_limit}")
        crew = GmailCrewAi().crew()
        result = crew.kickoff(inputs={"email_limit": email_limit})
        record.return_code = 0
        record.status = "completed"
        emit("[server] Crew run completed successfully")
    except Exception as e:
        record.return_code = 1
        record.status = "failed"
        emit(f"[server] ERROR: {e}")
    finally:
        record.ended_at = datetime.utcnow()
        # Restore stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr


def _mask_email(email_address: str) -> str:
    try:
        name, domain = email_address.split("@", 1)
        if len(name) <= 2:
            masked = name[0] + "*" * (len(name) - 1)
        else:
            masked = name[:2] + "*" * (len(name) - 2)
        return f"{masked}@{domain}"
    except Exception:
        return "***"


class RunRequest(BaseModel):
    email_address: str
    app_password: str
    email_limit: int | str = 5

    @field_validator("email_limit")
    @classmethod
    def _normalize_limit(cls, v):
        try:
            return int(v)
        except Exception:
            return 5


@app.post("/api/runs")
async def start_run(payload: RunRequest):
    email_address = payload.email_address.strip()
    app_password = payload.app_password.strip()
    email_limit = int(payload.email_limit)

    if not email_address or not app_password:
        raise HTTPException(status_code=400, detail="email_address and app_password are required")

    rec = run_manager.create_run(email_address)

    t = threading.Thread(target=_run_crew_thread, args=(rec, app_password, email_limit), daemon=True)
    t.start()

    return {"run_id": rec.id, "status": rec.status, "started_at": rec.started_at.isoformat() + "Z"}


@app.get("/api/runs")
async def list_runs():
    items = []
    for r in run_manager.list():
        items.append(
            {
                "id": r.id,
                "email_address": _mask_email(r.email_address),
                "status": r.status,
                "started_at": r.started_at.isoformat() + "Z",
                "ended_at": r.ended_at.isoformat() + "Z" if r.ended_at else None,
                "return_code": r.return_code,
                "log_lines": len(r.logs),
            }
        )
    return {"runs": items}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    try:
        r = run_manager.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "id": r.id,
        "email_address": _mask_email(r.email_address),
        "status": r.status,
        "started_at": r.started_at.isoformat() + "Z",
        "ended_at": r.ended_at.isoformat() + "Z" if r.ended_at else None,
        "return_code": r.return_code,
        "log_lines": len(r.logs),
    }


@app.get("/api/runs/{run_id}/logs")
async def get_logs(run_id: str, start: int = Query(0, ge=0)):
    try:
        r = run_manager.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    # Return logs from index 'start'
    with r._lock:
        lines = r.logs[start:]
        next_idx = start + len(lines)
        status = r.status
    return {"start": start, "next": next_idx, "status": status, "lines": lines}


@app.get("/api/output")
async def list_output_files():
    out_dir = os.path.abspath(os.path.join(os.getcwd(), "output"))
    files = []
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(out_dir, name)
            try:
                stat = os.stat(path)
                files.append(
                    {
                        "name": name,
                        "size": stat.st_size,
                        "modified": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                    }
                )
            except Exception:
                continue
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"directory": out_dir, "files": files}


@app.get("/api/output/{name}")
async def read_output_file(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="invalid filename")
    out_dir = os.path.abspath(os.path.join(os.getcwd(), "output"))
    path = os.path.join(out_dir, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    try:
        import json
        with open(path, "r") as f:
            data = json.load(f)
        return {"name": name, "content": data}
    except Exception:
        with open(path, "r", errors="ignore") as f:
            text = f.read()
        return {"name": name, "raw": text}


def _safe_load_json(path: str):
    try:
        import json
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


@app.get("/api/summary")
async def get_summary():
    """Aggregate key categories for UI: Deleted Emails, Read Only, Drafts."""
    out_dir = os.path.abspath(os.path.join(os.getcwd(), "output"))

    # Deleted Emails from cleanup_plan.json
    deleted_emails = []
    cleanup_plan = _safe_load_json(os.path.join(out_dir, "cleanup_plan.json"))
    if isinstance(cleanup_plan, dict):
        items = cleanup_plan.get("items") or []
        for it in items:
            try:
                if it.get("deleted") is True:
                    deleted_emails.append(
                        {
                            "email_id": it.get("email_id"),
                            "subject": it.get("subject"),
                            "sender": it.get("sender"),
                            "age_days": it.get("age_days"),
                            "reason": it.get("reason"),
                        }
                    )
            except Exception:
                continue

    # Read Only from categorization_report.json (required_action == READ_ONLY)
    read_only = []
    categorization = _safe_load_json(os.path.join(out_dir, "categorization_report.json"))
    if isinstance(categorization, dict):
        items = categorization.get("items") or categorization.get("emails") or []
        if isinstance(items, dict) and "items" in items:
            items = items["items"]
        if isinstance(items, list):
            for it in items:
                try:
                    action = (it.get("required_action") or "").upper()
                    category = (it.get("category") or "").upper()
                    if action == "READ_ONLY" or category == "YOUTUBE":
                        read_only.append(
                            {
                                "email_id": it.get("email_id"),
                                "subject": it.get("subject"),
                                "sender": it.get("sender"),
                                "category": it.get("category"),
                                "priority": it.get("priority"),
                            }
                        )
                except Exception:
                    continue

    # Drafts from response_plan.json items
    drafts = []
    response_plan = _safe_load_json(os.path.join(out_dir, "response_plan.json"))
    if isinstance(response_plan, dict):
        items = response_plan.get("items") or []
        if isinstance(items, list):
            for it in items:
                try:
                    drafts.append(
                        {
                            "email_id": it.get("email_id"),
                            "subject": it.get("subject"),
                            "recipient": it.get("recipient"),
                            "response_summary": it.get("response_summary"),
                            "draft_saved": it.get("draft_saved"),
                        }
                    )
                except Exception:
                    continue

    return {
        "deleted_emails": deleted_emails,
        "read_only": read_only,
        "drafts": drafts,
    }


def _resolve_static_dir() -> Optional[str]:
    candidates = []
    # Explicit override
    if os.environ.get("WEB_DIR"):
        candidates.append(os.environ["WEB_DIR"])
    # Repo-relative
    here = os.path.dirname(__file__)
    candidates.append(os.path.abspath(os.path.join(here, "..", "web")))  # src/web when running from source
    candidates.append(os.path.abspath(os.path.join(os.getcwd(), "src", "web")))
    candidates.append(os.path.abspath(os.path.join(os.getcwd(), "web")))
    # Package resource (when installed)
    try:
        import importlib.resources as ir
        files = ir.files("gmail_crew_ai").joinpath("web")
        candidates.append(str(files))
    except Exception:
        pass

    for p in candidates:
        if p and os.path.isdir(p):
            return p
    return None


static_dir = _resolve_static_dir()
if static_dir:
    print(f"[server] Serving static UI from: {static_dir}")
    app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="static")

    @app.get("/")
    async def _root_redirect():
        return RedirectResponse(url="/ui/")
else:
    print("[server] WARNING: Static UI directory not found; falling back to basic index page")

    @app.get("/", response_class=HTMLResponse)
    async def _fallback_root():
        return """
        <!doctype html>
        <meta charset='utf-8'>
        <meta name='viewport' content='width=device-width, initial-scale=1'>
        <title>Gmail Crew AI</title>
        <body style="font-family: system-ui, sans-serif; padding: 24px">
          <h1>Gmail Crew AI</h1>
          <p>The static UI was not found. You can still start runs via API:</p>
          <ul>
            <li>POST <code>/api/runs</code> with JSON { email_address, app_password, email_limit }</li>
            <li>GET <code>/api/runs</code> to list</li>
            <li>GET <code>/api/runs/&lt;id&gt;/logs</code> to tail logs</li>
          </ul>
          <p>Or set WEB_DIR environment variable to your web directory and restart.</p>
        </body>
        """

@app.get("/health")
async def health():
    return {"ok": True}


def main():
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("gmail_crew_ai.server:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
