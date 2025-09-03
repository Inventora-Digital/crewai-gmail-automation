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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from gmail_crew_ai.crew import GmailCrewAi
from dotenv import load_dotenv

# Load environment variables from .env for local development
load_dotenv()

# Firebase / GCP (optional) imports
_FIREBASE_READY = False
try:
    import firebase_admin
    from firebase_admin import auth as fb_auth, credentials as fb_credentials
    from google.cloud import firestore
    from google.cloud import secretmanager
    _FIREBASE_READY = True
except Exception:
    _FIREBASE_READY = False


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


def _run_crew_thread(record: RunRecord, app_password: str, email_limit: int, signature_name: Optional[str] = None, signature_block: Optional[str] = None) -> None:
    # Set environment variables for this thread execution
    # Note: This modifies process-wide env; ensure only one run at a time if that matters.
    os.environ["EMAIL_ADDRESS"] = record.email_address
    os.environ["APP_PASSWORD"] = app_password

    # Signature for this run (per-thread env)
    if signature_name:
        os.environ["SIGNATURE_NAME"] = signature_name
    if signature_block is not None:
        os.environ["EMAIL_SIGNATURE"] = signature_block

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


# -------------------------------
# Firebase / Firestore utilities
# -------------------------------

def _get_project_id() -> Optional[str]:
    pid = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if pid:
        return pid
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.isfile(cred_path):
        try:
            import json
            with open(cred_path, "r") as f:
                data = json.load(f)
            return data.get("project_id")
        except Exception:
            return None
    return None


def _init_firebase_once() -> bool:
    if not _FIREBASE_READY:
        return False
    try:
        if not firebase_admin._apps:
            cred = fb_credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": _get_project_id()})
        return True
    except Exception as e:
        print(f"[server] WARNING: Failed to initialize Firebase Admin: {e}")
        return False


def _firestore_client():
    if not _init_firebase_once():
        raise RuntimeError("Firebase Admin not initialized")
    return firestore.Client(project=_get_project_id())


def _secret_client():
    if not _init_firebase_once():
        raise RuntimeError("Firebase Admin not initialized")
    return secretmanager.SecretManagerServiceClient()


def _secret_name_for_user(uid: str) -> str:
    pid = _get_project_id()
    if not pid:
        raise RuntimeError("Project ID not found; set FIREBASE_PROJECT_ID or GOOGLE_APPLICATION_CREDENTIALS")
    return f"projects/{pid}/secrets/user-{uid}-gmail"


def _ensure_secret_exists(uid: str):
    client = _secret_client()
    name = _secret_name_for_user(uid)
    parent = f"projects/{_get_project_id()}"
    try:
        client.get_secret(request={"name": name})
    except Exception:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": f"user-{uid}-gmail",
                "secret": {"replication": {"automatic": {}}},
            }
        )
    return name


def _add_secret_version(uid: str, payload: str):
    # Try Secret Manager first; fallback to KMS-encrypted Firestore
    try:
        client = _secret_client()
        name = _ensure_secret_exists(uid)
        client.add_secret_version(
            request={
                "parent": name,
                "payload": {"data": payload.encode("utf-8")},
            }
        )
        return
    except Exception as e:
        pass

    # Fallback: KMS encrypt and store in Firestore
    ciphertext_b64, key_name = _kms_encrypt(payload)
    if ciphertext_b64:
        db = _firestore_client()
        ref = db.collection("users").document(uid)
        ref.set({
            "secret_ciphertext": ciphertext_b64,
            "secret_kms_key": key_name,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }, merge=True)


def _access_secret(uid: str) -> Optional[str]:
    # Try Secret Manager
    try:
        client = _secret_client()
        name = _secret_name_for_user(uid)
        version = f"{name}/versions/latest"
        resp = client.access_secret_version(request={"name": version})
        return resp.payload.data.decode("utf-8")
    except Exception:
        pass

    # Fallback: KMS decrypt from Firestore fields
    try:
        db = _firestore_client()
        ref = db.collection("users").document(uid)
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict() or {}
            ct = data.get("secret_ciphertext")
            key_name = data.get("secret_kms_key")
            if ct and key_name:
                return _kms_decrypt(ct, key_name)
    except Exception:
        pass
    return None


def _kms_key_name() -> Optional[str]:
    return os.environ.get("KMS_KEY")


def _kms_encrypt(plaintext: str) -> tuple[Optional[str], Optional[str]]:
    key_name = _kms_key_name()
    if not key_name:
        return None, None
    try:
        from google.cloud import kms
        kms_client = kms.KeyManagementServiceClient()
        resp = kms_client.encrypt(request={
            "name": key_name,
            "plaintext": plaintext.encode("utf-8"),
        })
        import base64
        return base64.b64encode(resp.ciphertext).decode("ascii"), key_name
    except Exception as e:
        print(f"[server] WARNING: KMS encrypt failed: {e}")
        return None, key_name


def _kms_decrypt(ciphertext_b64: str, key_name: str) -> Optional[str]:
    try:
        from google.cloud import kms
        import base64
        kms_client = kms.KeyManagementServiceClient()
        ct = base64.b64decode(ciphertext_b64.encode("ascii"))
        resp = kms_client.decrypt(request={
            "name": key_name,
            "ciphertext": ct,
        })
        return resp.plaintext.decode("utf-8")
    except Exception as e:
        print(f"[server] WARNING: KMS decrypt failed: {e}")
        return None


def _get_user_settings(uid: str) -> Dict[str, object]:
    db = _firestore_client()
    ref = db.collection("users").document(uid)
    doc = ref.get()
    data = doc.to_dict() if doc.exists else {}
    # Secret may be in Secret Manager or Firestore (KMS encrypted)
    has_secret = _access_secret(uid) is not None or bool(data.get("secret_ciphertext"))
    return {
        "email_address": data.get("email_address"),
        "auth_type": data.get("auth_type", "app_password"),
        "has_secret": has_secret,
        "updated_at": data.get("updated_at"),
        "signature_name": data.get("signature_name"),
        "signature": data.get("signature"),
    }


def _save_user_settings(uid: str, email_address: Optional[str], app_password: Optional[str], auth_type: str = "app_password"):
    db = _firestore_client()
    ref = db.collection("users").document(uid)
    updates: Dict[str, object] = {"updated_at": datetime.utcnow().isoformat() + "Z", "auth_type": auth_type}
    if email_address:
        updates["email_address"] = email_address
    ref.set(updates, merge=True)
    if app_password:
        _add_secret_version(uid, app_password)


def _get_user_from_request(request: Request, required: bool = False) -> Optional[Dict[str, str]]:
    if not _init_firebase_once():
        if required:
            raise HTTPException(status_code=401, detail="Firebase not configured")
        return None
    authz = request.headers.get("Authorization", "")
    token = None
    if authz.lower().startswith("bearer "):
        token = authz.split(" ", 1)[1].strip()
    if not token:
        if required:
            raise HTTPException(status_code=401, detail="Missing bearer token")
        return None
    try:
        decoded = fb_auth.verify_id_token(token)
        return {"uid": decoded.get("uid"), "email": decoded.get("email")}
    except Exception as e:
        if required:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
        return None


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
    email_address: Optional[str] = None
    app_password: Optional[str] = None
    email_limit: int | str = 5

    @field_validator("email_limit")
    @classmethod
    def _normalize_limit(cls, v):
        try:
            return int(v)
        except Exception:
            return 5


@app.post("/api/runs")
async def start_run(payload: RunRequest, request: Request):
    user = _get_user_from_request(request, required=False)
    email_address = (payload.email_address or "").strip()
    app_password = (payload.app_password or "").strip()
    email_limit = int(payload.email_limit)
    signature_name = None
    signature_block = None

    # Try to hydrate from user settings if authenticated
    if user and (not email_address or not app_password):
        try:
            settings = _get_user_settings(user["uid"]) if _init_firebase_once() else {}
            if not email_address:
                email_address = (settings.get("email_address") or "").strip()
            if not app_password and settings.get("has_secret"):
                secret = _access_secret(user["uid"]) or ""
                app_password = secret.strip()
            # prepare signature
            signature_name = settings.get("signature_name") if isinstance(settings, dict) else None
            signature_block = settings.get("signature") if isinstance(settings, dict) else None
        except Exception as e:
            # Non-fatal; fall through to explicit payload requirement
            pass

    # Final fallback: allow environment defaults for headless/agent invocations
    if not email_address:
        email_address = os.environ.get("DEFAULT_EMAIL_ADDRESS", "").strip()
    if not app_password:
        app_password = os.environ.get("DEFAULT_APP_PASSWORD", "").strip()

    if not email_address or not app_password:
        raise HTTPException(status_code=400, detail="email_address and app_password are required (or configure DEFAULT_EMAIL_ADDRESS/DEFAULT_APP_PASSWORD env vars)")

    rec = run_manager.create_run(email_address)
    t = threading.Thread(target=_run_crew_thread, args=(rec, app_password, email_limit, signature_name, signature_block), daemon=True)
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


class SettingsBody(BaseModel):
    email_address: Optional[str] = Field(None)
    app_password: Optional[str] = Field(None)
    signature_name: Optional[str] = Field(None)
    signature: Optional[str] = Field(None)
    auth_type: str = Field("app_password")


@app.get("/api/me/settings")
async def get_me_settings(request: Request):
    user = _get_user_from_request(request, required=True)
    if not _init_firebase_once():
        raise HTTPException(status_code=500, detail="Firebase not configured")
    return _get_user_settings(user["uid"]) 


@app.put("/api/me/settings")
async def put_me_settings(body: SettingsBody, request: Request):
    user = _get_user_from_request(request, required=True)
    if not _init_firebase_once():
        raise HTTPException(status_code=500, detail="Firebase not configured")
    _save_user_settings(user["uid"], body.email_address, body.app_password, body.auth_type)
    # Save signature fields to Firestore
    try:
        db = _firestore_client()
        ref = db.collection("users").document(user["uid"])
        updates = {}
        if body.signature_name is not None:
            updates["signature_name"] = body.signature_name
        if body.signature is not None:
            updates["signature"] = body.signature
        if updates:
            ref.set(updates, merge=True)
    except Exception as e:
        # Non-fatal; omit
        pass
    return _get_user_settings(user["uid"]) 


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
        # Safe fallback if server was restarted or run is unknown to this process
        return {"start": start, "next": start, "status": "unknown", "lines": []}
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
    """Aggregate key categories for UI: Deleted, Not Deleted, Drafts."""
    out_dir = os.path.abspath(os.path.join(os.getcwd(), "output"))

    # Deleted / Not Deleted from cleanup_plan.json
    deleted = []
    not_deleted = []
    cleanup_plan = _safe_load_json(os.path.join(out_dir, "cleanup_plan.json"))
    if isinstance(cleanup_plan, dict):
        items = cleanup_plan.get("items") or []
        for it in items:
            try:
                entry = {
                    "email_id": it.get("email_id"),
                    "subject": it.get("subject"),
                    "sender": it.get("sender"),
                    "age_days": it.get("age_days"),
                    "reason": it.get("reason"),
                    "category": it.get("category"),
                    "priority": it.get("priority"),
                }
                if bool(it.get("deleted")):
                    deleted.append(entry)
                else:
                    not_deleted.append(entry)
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

    return {"deleted": deleted, "not_deleted": not_deleted, "drafts": drafts}


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


@app.get("/api/firebase-config")
async def firebase_config():
    pid = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
    api_key = os.environ.get("FIREBASE_WEB_API_KEY", "")
    auth_domain = os.environ.get("FIREBASE_AUTH_DOMAIN") or (f"{pid}.firebaseapp.com" if pid else "")
    return {"apiKey": api_key, "authDomain": auth_domain, "projectId": pid}


@app.get("/api/whoami")
async def whoami(request: Request):
    user = _get_user_from_request(request, required=False)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, **user}


def main():
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("gmail_crew_ai.server:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
