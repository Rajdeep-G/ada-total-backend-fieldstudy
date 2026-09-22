# import os
import json
import signal
import os
import sys
import random
import string
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import zipfile
import io

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
#  CONFIG
# ============================================================

# --- Extension telemetry dirs ---
PING_LOG_DIR  = os.getenv("PING_LOG_DIR",  os.path.abspath("./ping_logs"))
EVENT_LOG_DIR = os.getenv("EVENT_LOG_DIR", os.path.abspath("./logs"))

# --- Survey data dirs ---
SURVEY_DATA_DIR     = os.getenv("SURVEY_DATA_DIR", os.path.abspath("./survey_data"))
SURVEY_PROGRESS_DIR = os.path.join(SURVEY_DATA_DIR, "progress")  # one JSON file per prolific_id
PARTICIPANTS_FILE   = os.path.join(SURVEY_DATA_DIR, "participants.ndjson")
SUBMISSIONS_FILE    = os.path.join(SURVEY_DATA_DIR, "submissions.ndjson")

# --- Shared ---
CODES_FILE     = os.getenv("CODES_FILE",     os.path.abspath("./codes.json"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "2000000"))  # 2 MB
ALLOW_ORIGINS  = os.getenv("ALLOW_ORIGINS", "*").split(",")

os.makedirs(EVENT_LOG_DIR,       exist_ok=True)
os.makedirs(PING_LOG_DIR,        exist_ok=True)
os.makedirs(SURVEY_DATA_DIR,     exist_ok=True)
os.makedirs(SURVEY_PROGRESS_DIR, exist_ok=True)


# ============================================================
#  SHARED: CODE STORE  (in-memory dict backed by codes.json)
#
#  Extension /ping  → writes new codes (mutates _codes + persists to disk)
#  Survey /survey/verify-code → reads _codes directly (no disk I/O needed)
#
#  Because both live in the same process, _codes is always up-to-date.
#  Structure: { "AB3X9K": "uuid-string", ... }
# ============================================================

def _load_codes() -> Dict[str, str]:
    try:
        with open(CODES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_codes(codes: Dict[str, str]) -> None:
    """Atomic write via .tmp + rename — safe against mid-write crashes."""
    tmp = CODES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CODES_FILE)

def _generate_code(existing_codes: Dict[str, str]) -> str:
    """6-char uppercase alphanumeric, collision-checked against existing keys."""
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(100):
        code = "".join(random.choices(alphabet, k=6))
        if code not in existing_codes:
            return code
    raise RuntimeError("Could not generate a unique code after 100 attempts")

# Loaded once at startup; /ping mutates it in-place and persists to disk.
_codes: Dict[str, str] = _load_codes()  # code → uuid


# ============================================================
#  SHARED: GENERIC HELPERS
# ============================================================

SENSITIVE_KEYS = {"password", "ssn", "card", "cvv", "email", "phone",
                  "address", "query", "search", "token"}

def _append_ndjson(path: str, record: Dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())

def _read_all_ndjson(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return rows

def get_client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None

def tail_lines(path: str, n: int) -> List[str]:
    to_read = max(n * 256, 4096)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - to_read))
            data = f.read().decode("utf-8", errors="ignore")
    except FileNotFoundError:
        return []
    lines = data.splitlines()
    return lines[-n:] if len(lines) > n else lines


# ============================================================
#  EXTENSION-SIDE HELPERS
# ============================================================

def append_line_events(uuid: str, record: Dict[str, Any]) -> None:
    _append_ndjson(os.path.join(EVENT_LOG_DIR, f"events_{uuid}.ndjson"), record)

def append_line_pings(uuid: str, record: Dict[str, Any]) -> None:
    _append_ndjson(os.path.join(PING_LOG_DIR, f"pings_{uuid}.ndjson"), record)


# ============================================================
#  SURVEY-SIDE HELPERS
# ============================================================

def _progress_path(prolific_id: str) -> str:
    safe = prolific_id.replace("/", "_").replace("..", "_")
    return os.path.join(SURVEY_PROGRESS_DIR, f"progress_{safe}.json")

def _load_progress(prolific_id: str) -> Dict[str, Any]:
    try:
        with open(_progress_path(prolific_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_progress(prolific_id: str, data: Dict[str, Any]) -> None:
    tmp = _progress_path(prolific_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _progress_path(prolific_id))

def _is_completed(prolific_id: str) -> bool:
    return any(r.get("prolific_id") == prolific_id
               for r in _read_all_ndjson(SUBMISSIONS_FILE))

def _participant_exists(prolific_id: str) -> bool:
    return any(r.get("prolific_id") == prolific_id
               for r in _read_all_ndjson(PARTICIPANTS_FILE))


# ============================================================
#  SIGNAL HANDLER
# ============================================================

def _signal_handler(sig, frame):
    print("\nReceived interrupt signal. Shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ============================================================
#  APP + MIDDLEWARE
# ============================================================

app = FastAPI(title="ada-study-server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ============================================================
#  PYDANTIC MODELS — Extension
# ============================================================

class Ok(BaseModel):
    ok: bool = True

class ToggleIn(BaseModel):
    participant_uuid: str = Field(..., max_length=64)
    at: Optional[datetime] = None
    t_flag: Optional[int] = None
    extras: Optional[Dict[str, Any]] = None

class EventIn(BaseModel):
    participant_uuid: str = Field(..., max_length=64)
    event_type: str       = Field(..., max_length=32)
    client_time: Optional[datetime] = None
    action: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None

class InstallIn(BaseModel):
    participant_uuid: str = Field(..., max_length=64)
    installed_at: Optional[datetime] = None
    t_flag: Optional[int] = None
    extras: Optional[Dict[str, Any]] = None

class PingIn(BaseModel):
    participant_uuid: str = Field(..., max_length=64)
    at: Optional[datetime] = None
    extras: Optional[Dict[str, Any]] = None

class PingOut(BaseModel):
    ok: bool = True
    code: Optional[str] = None  # non-null only on the very first ping for this UUID


# ============================================================
#  PYDANTIC MODELS — Survey
# ============================================================

class SurveyStartIn(BaseModel):
    prolific_id: str = Field(..., max_length=64)

class SurveyProgressIn(BaseModel):
    prolific_id: str = Field(..., max_length=64)
    subsection: str  = Field(..., max_length=32)  # matches page name in survey.json
    answers: Dict[str, Any]

class VerifyCodeIn(BaseModel):
    prolific_id: str = Field(..., max_length=64)
    code: str        = Field(..., min_length=6, max_length=6)

class VerifyCodeResp(BaseModel):
    ok: bool
    message: str
    uuid: Optional[str] = None  # returned on success for audit

class SurveySubmitIn(BaseModel):
    prolific_id: str = Field(..., max_length=64)
    answers: Dict[str, Any]


# ============================================================
#  ROUTES — Shared
# ============================================================

@app.get("/healthz", response_model=Ok)
async def healthz():
    return Ok()


# ============================================================
#  ROUTES — Extension telemetry
#  Flat paths (no prefix) — consumed only by the Chrome extension.
# ============================================================

ALLOWED_ACTIONS = {
    "parsing_triggered",
    "refetch_triggered",
    "delete_activity_clicked",
    "deleted_activity_confirmed",
    "sensitivity_feedback",
    "retrain_model_withfeedback",
    "filter_modal_open",
    "date_filter_submit",
    "type_filter_submit",
    "filter_removal",
    "download model",
}

@app.get("/actions")
async def list_actions():
    return sorted(ALLOWED_ACTIONS)


@app.post("/install", response_model=Ok)
async def install(request: Request, payload: InstallIn):
    uuid = payload.participant_uuid.strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="participant_uuid required")

    path = os.path.join(EVENT_LOG_DIR, f"events_{uuid}.ndjson")
    if not os.path.exists(path):
        open(path, "a").close()  # touch
    print(payload.dict())
    rec = {
        "participant_uuid": uuid,
        "event_type":       "install",
        "flag":             payload.t_flag,
        "client_time":      payload.installed_at.isoformat() if payload.installed_at else None,
        "client_ip":        get_client_ip(request),
        "server_received":  datetime.now(timezone.utc).isoformat(),
    }
    append_line_events(uuid, rec)
    return Ok()


@app.post("/enable", response_model=Ok)
async def enable(request: Request, payload: ToggleIn):
    uuid = payload.participant_uuid.strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="participant_uuid required")

    rec = {
        "participant_uuid": uuid,
        "event_type":       "enable",
        "flag":             payload.t_flag,
        "client_time":      payload.at.isoformat() if payload.at else None,
        "client_ip":        get_client_ip(request),
        "server_received":  datetime.now(timezone.utc).isoformat(),
    }
    append_line_events(uuid, rec)
    return Ok()


@app.post("/events", response_model=Ok)
async def log_event(request: Request, payload: EventIn):
    try:
        clen = int(request.headers.get("content-length", "0"))
    except Exception:
        clen = 0
    if clen > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    uuid = payload.participant_uuid.strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="participant_uuid required")

    action_name = payload.action.strip() if payload.action else None
    if action_name and action_name not in ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{action_name}'. Allowed: {sorted(ALLOWED_ACTIONS)}",
        )

    rec: Dict[str, Any] = {
        "participant_uuid": uuid,
        "event_type":       payload.event_type,
        "client_time":      payload.client_time.isoformat() if payload.client_time else None,
        "action":           action_name,
        "params":           payload.params,
        "extras":           payload.extras,
        "client_ip":        get_client_ip(request),
        "server_received":  datetime.now(timezone.utc).isoformat(),
    }
    for key in ("params", "extras"):
        if isinstance(rec.get(key), dict):
            for k in list(rec[key].keys()):
                if k.lower() in SENSITIVE_KEYS:
                    rec[key].pop(k, None)

    append_line_events(uuid, rec)
    return Ok()


@app.post("/ping", response_model=PingOut)
async def ping(request: Request, payload: PingIn):
    global _codes

    uuid = payload.participant_uuid.strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="participant_uuid required")

    # First-ping detection — O(n) scan, fine at study scale (≤35 UUIDs)
    existing_code = next((c for c, u in _codes.items() if u == uuid), None)
    issued_code: Optional[str] = None

    if existing_code is None:
        issued_code = _generate_code(_codes)
        _codes[issued_code] = uuid
        _save_codes(_codes)  # atomic write to codes.json

    rec = {
        "participant_uuid": uuid,
        "event_type":       "ping",
        "client_time":      payload.at.isoformat() if payload.at else None,
        "client_ip":        get_client_ip(request),
        "server_received":  datetime.now(timezone.utc).isoformat(),
        "code_issued":      issued_code,  # audit trail; null on subsequent pings
    }
    append_line_pings(uuid, rec)
    return PingOut(ok=True, code=issued_code)


@app.get("/tail")
async def tail(
    uuid: str = Query(..., max_length=64),
    n:    int = Query(50, ge=1, le=5000),
):
    path  = os.path.join(EVENT_LOG_DIR, f"events_{uuid}.ndjson")
    lines = tail_lines(path, n)
    out   = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


@app.get("/uninstall", response_class=HTMLResponse)
async def uninstall_get(
    request: Request,
    participant_uuid: str = Query(..., max_length=64),
    installed_at: Optional[str] = None,
):
    uuid = participant_uuid.strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="participant_uuid required")

    rec = {
        "participant_uuid": uuid,
        "event_type":       "uninstall",
        "client_time":      installed_at,
        "client_ip":        get_client_ip(request),
        "server_received":  datetime.now(timezone.utc).isoformat(),
    }
    append_line_events(uuid, rec)

    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="en">
<head>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg width='128' height='128' viewBox='0 0 128 128' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M64 4 L116 24 L116 68 Q116 106 64 124 Q12 106 12 68 L12 24 Z' fill='%234f46e5'/%3E%3Cpath d='M64 14 L106 30 L106 68 Q106 100 64 116 Q22 100 22 68 L22 30 Z' fill='%234338ca'/%3E%3Crect x='32' y='70' width='18' height='34' rx='3' fill='%23a5f3fc'/%3E%3Crect x='55' y='55' width='18' height='49' rx='3' fill='%2367e8f9'/%3E%3Crect x='78' y='40' width='18' height='64' rx='3' fill='%23ffffff'/%3E%3C/svg%3E">
  <meta charset="UTF-8">
  <title>Thank You - ADA Study</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           display:flex; align-items:center; justify-content:center;
           min-height:100vh; margin:0; background:#f5f5f5; color:#222; }
    .card { background:white; border-radius:12px; padding:48px 56px; max-width:480px;
            text-align:center; box-shadow:0 4px 24px rgba(0,0,0,0.08); }
    h1 { font-size:1.6rem; margin:0 0 12px; }
    p  { color:#555; line-height:1.6; margin:0 0 10px; }
    .small { font-size:0.85rem; color:#999; margin-top:24px; }
  </style>
</head>
<body>
  <div class="card">
    <svg width="64" height="64" viewBox="0 0 128 128" xmlns="http://www.w3.org/2000/svg" style="margin-bottom:12px;">
      <path d="M64 4 L116 24 L116 68 Q116 106 64 124 Q12 106 12 68 L12 24 Z" fill="#4f46e5"/>
      <path d="M64 14 L106 30 L106 68 Q106 100 64 116 Q22 100 22 68 L22 30 Z" fill="#4338ca"/>
      <rect x="32" y="70" width="18" height="34" rx="3" fill="#a5f3fc"/>
      <rect x="55" y="55" width="18" height="49" rx="3" fill="#67e8f9"/>
      <rect x="78" y="40" width="18" height="64" rx="3" fill="#ffffff"/>
      <text x="64" y="118" text-anchor="middle" fill="white" font-family="sans-serif"
            font-weight="700" font-size="18px" letter-spacing="3px">ADA</text>
    </svg>
    <h1>Thank you for participating!</h1>
    <p>Your contribution to the ADA study is greatly appreciated.</p>
    <p>The extension has been removed successfully.</p>
    <p class="small">If you have any questions, please reach out to the research team at
      <a href="mailto:ghoshrajdeep200025@kgpian.iitkgp.ac.in">ghoshrajdeep200025@kgpian.iitkgp.ac.in</a>.
    </p>
    <button onclick="window.close()" style="margin-top:28px; padding:10px 28px; border:none;
      border-radius:8px; background:#4f46e5; color:white; font-size:0.95rem; cursor:pointer;">
      Close this tab
    </button>
  </div>
</body>
</html>
""", status_code=200)


# ============================================================
#  ROUTES — Survey
#  All prefixed with /survey/ — consumed by the React survey frontend.
# ============================================================

@app.get("/survey/check")
async def survey_check(prolific_id: str = Query(..., max_length=64)):
    """Has this prolific_id already submitted? Called on the Landing page."""
    pid = prolific_id.strip()
    if not pid:
        raise HTTPException(status_code=400, detail="prolific_id required")
    return {"exists": _is_completed(pid)}


@app.post("/survey/start", response_model=Ok)
async def survey_start(payload: SurveyStartIn):
    """
    Called when user enters their Prolific ID and clicks Start.
    Records first arrival in participants.ndjson. Idempotent.
    """
    pid = payload.prolific_id.strip()
    if not pid:
        raise HTTPException(status_code=400, detail="prolific_id required")
    if _is_completed(pid):
        raise HTTPException(status_code=409, detail="Already completed")
    if not _participant_exists(pid):
        _append_ndjson(PARTICIPANTS_FILE, {
            "prolific_id": pid,
            "arrived_at":  datetime.now(timezone.utc).isoformat(),
        })
    return Ok()


@app.post("/survey/progress", response_model=Ok)
async def survey_progress(payload: SurveyProgressIn):
    """
    Called after the user advances past each subsection.
    Merges new answers into the per-user progress file — cumulative,
    never wipes previous answers. Safe to call multiple times per subsection.
    """
    pid = payload.prolific_id.strip()
    if not pid:
        raise HTTPException(status_code=400, detail="prolific_id required")
    if _is_completed(pid):
        return Ok()  # silently succeed — don't touch a completed submission

    progress = _load_progress(pid)
    progress["prolific_id"]     = pid
    progress["last_subsection"] = payload.subsection
    progress["last_saved_at"]   = datetime.now(timezone.utc).isoformat()

    existing = progress.get("answers", {})
    existing.update(payload.answers)
    progress["answers"] = existing

    _save_progress(pid, progress)
    return Ok()


@app.post("/survey/verify-code", response_model=VerifyCodeResp)
async def survey_verify_code(payload: VerifyCodeIn):
    """
    Called after subsection 6 when the user enters their ADA code.
    Uses the shared in-memory _codes dict directly — no disk read needed,
    since /ping keeps _codes current in this same process.
    On success, writes ada_code + ada_uuid into the progress file.
    """
    pid  = payload.prolific_id.strip()
    code = payload.code.strip().upper()
    
    if not pid or not code:
        raise HTTPException(status_code=400, detail="prolific_id and code required")

    uuid = _codes.get(code)  # O(1) in-memory lookup
    if not uuid:
        return VerifyCodeResp(ok=False, message="Code not recognised. Please check and try again.")

    progress = _load_progress(pid)
    progress["prolific_id"]      = pid
    progress["ada_code"]         = code
    progress["ada_uuid"]         = uuid
    progress["code_verified_at"] = datetime.now(timezone.utc).isoformat()
    _save_progress(pid, progress)

    return VerifyCodeResp(ok=True, message="Code verified successfully.", uuid=uuid)


@app.post("/survey/submit", response_model=Ok)
async def survey_submit(payload: SurveySubmitIn):
    """
    Called on final completion (after subsection 9).
    Merges all partial answers + ada_code/uuid from progress into
    submissions.ndjson, then deletes the progress file.
    Idempotent — a duplicate submit is silently ignored.
    """
    pid = payload.prolific_id.strip()
    if not pid:
        raise HTTPException(status_code=400, detail="prolific_id required")
    if _is_completed(pid):
        return Ok()

    progress = _load_progress(pid)
    final_answers = progress.get("answers", {})
    final_answers.update(payload.answers)  # final page answers take priority

    _append_ndjson(SUBMISSIONS_FILE, {
        "prolific_id":  pid,
        "ada_code":     progress.get("ada_code"),
        "ada_uuid":     progress.get("ada_uuid"),
        "answers":      final_answers,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })

    try:
        os.remove(_progress_path(pid))
    except FileNotFoundError:
        pass

    return Ok()


# ============================================================
#  ROUTES — Admin
# ============================================================

@app.get("/download-logs")
async def download_logs():
    """
    Bundles everything into a single zip:
      logs/         — extension event logs (per-uuid .ndjson)
      ping_logs/    — extension ping logs (per-uuid .ndjson)
      survey_data/  — participants.ndjson, submissions.ndjson, progress/
      codes.json    — shared code <-> uuid mapping
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Extension logs
        for folder in [EVENT_LOG_DIR, PING_LOG_DIR]:
            if not os.path.isdir(folder):
                continue
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                zf.write(filepath, arcname=os.path.join(os.path.basename(folder), filename))

        # Survey data (walks subdirs including progress/)
        for root, _, files in os.walk(SURVEY_DATA_DIR):
            for filename in files:
                filepath = os.path.join(root, filename)
                arcname  = os.path.relpath(filepath, start=os.path.dirname(SURVEY_DATA_DIR))
                zf.write(filepath, arcname=arcname)

        # Shared codes mapping
        if os.path.isfile(CODES_FILE):
            zf.write(CODES_FILE, arcname="codes.json")

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=all_logs.zip"},
    )