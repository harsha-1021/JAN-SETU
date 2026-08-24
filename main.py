"""Citizen Demand Intake API.

Primary cloud pipeline:
voice/text/photo -> Google Speech and Translation -> Gemini structured analysis
-> Firebase live operations + BigQuery analytics. SQLite and Sarvam remain
optional local fallbacks so the project is still runnable as a Digital Public
Good without a cloud account.
"""

import asyncio
import hashlib
import hmac
import io
import math
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timezone
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

load_dotenv()

import scoring
from bigquery_analytics import BigQueryAnalytics
from firebase_bridge import FirebaseBridge
from google_cloud_services import ComplaintAnalysis, GoogleCloudServices, PolicyBrief

app = FastAPI(title="Citizen Demand Intake API")

# Each open policymaker page owns a one-item queue. Mutations publish a small
# refresh signal; the browser then fetches the authoritative ranked data.
DASHBOARD_SUBSCRIBERS = set()


def publish_dashboard_event(event_type: str) -> None:
    for queue in tuple(DASHBOARD_SUBSCRIBERS):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event_type)
        except asyncio.QueueFull:
            pass

# Google services are the primary production path. Clients initialize lazily,
# so importing the application never requires cloud credentials.
google_cloud = GoogleCloudServices()
firebase = FirebaseBridge()
bigquery_analytics = BigQueryAnalytics()

# Optional Indian-language fallback retained for local/offline demonstrations.
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "").strip()
sarvam_client = None
if SARVAM_API_KEY:
    try:
        from sarvamai import SarvamAI

        sarvam_client = SarvamAI(api_subscription_key=SARVAM_API_KEY)
    except Exception:
        sarvam_client = None

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get(
    "SQLITE_PATH",
    "/tmp/complaints.db" if os.environ.get("K_SERVICE") else "complaints.db",
)
TRACKING_ALPHABET = string.ascii_uppercase + string.digits
NOMINATIM_HEADERS = {"User-Agent": "citizen-demand-prototype/1.0"}


def generate_tracking_code() -> str:
    return "CP-" + "".join(secrets.choice(TRACKING_ALPHABET) for _ in range(10))


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            citizen_id TEXT,
            original_text TEXT,
            translated_text TEXT,
            source_lang TEXT,
            category TEXT,
            severity_score REAL,
            latitude REAL,
            longitude REAL,
            region TEXT,
            status TEXT DEFAULT 'submitted',
            created_at TEXT,
            tracking_code TEXT
        )
        """
    )

    # Lightweight auto-migration: if complaints.db already existed from
    # before a schema change, add any missing columns instead of failing.
    # Keeps you from having to manually delete the db file every time
    # you add a field while prototyping.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(complaints)")}
    expected_columns = {
        "status": "TEXT DEFAULT 'submitted'",
        "tracking_code": "TEXT",
        "ai_summary": "TEXT DEFAULT ''",
        "infrastructure_need": "TEXT DEFAULT ''",
        "responsible_department": "TEXT DEFAULT ''",
        "ai_confidence": "REAL DEFAULT 0",
        "image_evidence": "TEXT",
        "ai_provider": "TEXT DEFAULT 'local-fallback'",
    }
    for column, definition in expected_columns.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE complaints ADD COLUMN {column} {definition}")

    missing_codes = conn.execute(
        "SELECT id FROM complaints WHERE tracking_code IS NULL OR tracking_code = ''"
    ).fetchall()
    for (complaint_id,) in missing_codes:
        conn.execute(
            "UPDATE complaints SET tracking_code = ? WHERE id = ?",
            (generate_tracking_code(), complaint_id),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_complaints_tracking_code "
        "ON complaints(tracking_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_complaints_region_category_citizen "
        "ON complaints(region, category, citizen_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT,
            category TEXT,
            forwarded_to TEXT,
            note TEXT,
            status TEXT DEFAULT 'forwarded',
            created_at TEXT
        )
        """
    )

    existing_escalation_columns = {row[1] for row in conn.execute("PRAGMA table_info(escalations)")}
    expected_escalation_columns = {
        "status": "TEXT DEFAULT 'forwarded'",
    }
    for column, definition in expected_escalation_columns.items():
        if column not in existing_escalation_columns:
            conn.execute(f"ALTER TABLE escalations ADD COLUMN {column} {definition}")

    # Policymaker login: users table stores a salted hash, never the raw
    # password. Sessions table backs an HttpOnly cookie so login state is
    # checked server-side on every protected request, not trusted from
    # the client.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT
        )
        """
    )

    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# Policymaker authentication
# ---------------------------------------------------------------------------
SESSION_COOKIE_NAME = "policymaker_session"


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple:
    """PBKDF2-SHA256, stdlib only — no extra dependency to install.
    Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return digest.hex(), salt.hex()


def configure_policymaker_from_environment() -> None:
    """Create or refresh the demo policymaker from private host secrets."""
    username = os.getenv("POLICYMAKER_USERNAME", "").strip()
    password = os.getenv("POLICYMAKER_PASSWORD", "")
    if not username or not password:
        return
    if len(password) < 12:
        raise RuntimeError("POLICYMAKER_PASSWORD must contain at least 12 characters")

    password_hash, password_salt = hash_password(password)
    created_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO users (username, password_hash, password_salt, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            password_hash = excluded.password_hash,
            password_salt = excluded.password_salt
        """,
        (username, password_hash, password_salt, created_at),
    )
    conn.commit()
    conn.close()


configure_policymaker_from_environment()


def verify_password(password: str, stored_hash: str, stored_salt_hex: str) -> bool:
    salt = bytes.fromhex(stored_salt_hex)
    candidate_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate_hash, stored_hash)


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO sessions (token, username, created_at) VALUES (?, ?, ?)",
        (token, username, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return token


def get_session_username(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT username FROM sessions WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row[0] if row else None


def require_auth(request: Request) -> str:
    """Verify a Firebase policymaker token, with local cookie fallback."""
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        claims = firebase.verify_policymaker_token(authorization[7:].strip())
        if claims:
            return claims.get("email") or claims.get("uid") or "policymaker"

    # Once the Firebase web app is configured, do not silently downgrade a
    # protected cloud deployment to the legacy local-cookie identity system.
    if firebase.public_config()["enabled"]:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = request.cookies.get(SESSION_COOKIE_NAME)
    username = get_session_username(token)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


class LoginResponse(BaseModel):
    username: str


@app.post("/auth/login", response_model=LoginResponse)
def login(username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT password_hash, password_salt FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()

    if row is None or not verify_password(password, row[0], row[1]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    token = create_session(username)
    resp = JSONResponse(content={"username": username})
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,  # 12 hours - fine for a demo/prototype
    )
    return resp


@app.post("/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    resp = JSONResponse(content={"status": "logged out"})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/auth/me", response_model=LoginResponse)
def me(username: str = Depends(require_auth)):
    return LoginResponse(username=username)


def store_complaint(
    citizen_id: str,
    original_text: str,
    translated_text: str,
    source_lang: str,
    analysis: ComplaintAnalysis,
    latitude: float,
    longitude: float,
    region: str,
) -> dict:
    tracking_code = generate_tracking_code()
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        allocated_id = firebase.allocate_id("complaints")
    except Exception:
        allocated_id = None
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """
        INSERT INTO complaints
            (id, citizen_id, original_text, translated_text, source_lang,
             category, severity_score, latitude, longitude, region, status,
             created_at, tracking_code, ai_summary, infrastructure_need,
             responsible_department, ai_confidence, image_evidence, ai_provider)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            allocated_id,
            citizen_id, original_text, translated_text, source_lang,
            analysis.category, analysis.severity_score, latitude, longitude, region,
            created_at, tracking_code, analysis.summary, analysis.infrastructure_need,
            analysis.responsible_department, analysis.confidence,
            analysis.image_evidence, analysis.provider,
        ),
    )
    conn.commit()
    complaint_id = cur.lastrowid
    conn.close()

    record = {
        "id": complaint_id,
        "tracking_code": tracking_code,
        "citizen_id": citizen_id,
        "original_text": original_text,
        "translated_text": translated_text,
        "source_lang": source_lang,
        "category": analysis.category,
        "severity_score": analysis.severity_score,
        "latitude": latitude,
        "longitude": longitude,
        "region": region,
        "status": "submitted",
        "created_at": created_at,
        "ai_summary": analysis.summary,
        "infrastructure_need": analysis.infrastructure_need,
        "responsible_department": analysis.responsible_department,
        "ai_confidence": analysis.confidence,
        "image_evidence": analysis.image_evidence,
        "ai_provider": analysis.provider,
    }
    try:
        firebase.save_complaint(record)
    except Exception:
        pass
    try:
        bigquery_analytics.insert_complaint(record)
    except Exception:
        pass
    publish_dashboard_event("refresh")
    return record


class ComplaintResponse(BaseModel):
    id: int
    tracking_code: str
    original_text: str
    translated_text: str
    source_lang: str
    category: str
    severity_score: float
    ai_summary: str
    infrastructure_need: str
    responsible_department: str
    ai_confidence: float
    image_evidence: Optional[str] = None
    ai_provider: str
    status: str = "submitted"


class ComplaintStatus(BaseModel):
    id: int
    tracking_code: str
    category: str
    status: str
    region: str
    created_at: str


async def read_photo(photo: Optional[UploadFile]) -> tuple:
    if photo is None or not photo.filename:
        return None, None
    mime_type = (photo.content_type or "").split(";")[0].lower()
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Photo must be JPEG, PNG, or WebP")
    content = await photo.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo must be smaller than 5 MB")
    return content, mime_type


def translate_to_english(text: str, source_language: str = "auto") -> tuple:
    try:
        return google_cloud.translate_text(text, source_language)
    except Exception:
        if sarvam_client is not None:
            try:
                translation = sarvam_client.text.translate(
                    input=text,
                    source_language_code="auto",
                    target_language_code="en-IN",
                )
                return (
                    translation.translated_text,
                    getattr(translation, "source_language_code", source_language),
                )
            except Exception:
                pass
    return text, source_language if source_language != "auto" else "unknown"


# ---------------------------------------------------------------------------
# Endpoint: voice complaint (audio upload)
# ---------------------------------------------------------------------------
@app.post("/complaints/voice", response_model=ComplaintResponse)
async def submit_voice_complaint(
    audio: UploadFile = File(...),
    photo: Optional[UploadFile] = File(None),
    citizen_id: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    region: str = Form(...),
    language_code: str = Form("hi-IN"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="The audio recording is empty")
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio must be smaller than 10 MB")

    try:
        original_text, source_lang = google_cloud.transcribe_audio(
            audio_bytes,
            audio.content_type or "audio/webm",
            language_code,
        )
        translated_text, detected_lang = translate_to_english(original_text, source_lang)
        source_lang = detected_lang or source_lang
    except Exception as google_error:
        if sarvam_client is None:
            raise HTTPException(
                status_code=502,
                detail="Google Speech-to-Text could not process this recording",
            ) from google_error
        try:
            stt_response = sarvam_client.speech_to_text.transcribe(
                file=io.BytesIO(audio_bytes),
                model="saaras:v3",
                mode="translate",
            )
            original_text = "(voice complaint)"
            translated_text = stt_response.transcript
            source_lang = getattr(stt_response, "language_code", language_code)
        except Exception as fallback_error:
            raise HTTPException(status_code=502, detail="Speech recognition failed") from fallback_error

    image_bytes, image_mime_type = await read_photo(photo)
    analysis = google_cloud.analyze_complaint(
        translated_text, image_bytes=image_bytes, image_mime_type=image_mime_type
    )
    record = store_complaint(
        citizen_id, original_text, translated_text, source_lang,
        analysis, latitude, longitude, region,
    )

    return ComplaintResponse(**record)


# ---------------------------------------------------------------------------
# Endpoint: text complaint (e.g. from a Telegram/WhatsApp bot)
# ---------------------------------------------------------------------------
@app.post("/complaints/text", response_model=ComplaintResponse)
async def submit_text_complaint(
    text: str = Form(...),
    photo: Optional[UploadFile] = File(None),
    citizen_id: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    region: str = Form(...),
):
    translated_text, source_lang = translate_to_english(text)
    image_bytes, image_mime_type = await read_photo(photo)
    analysis = google_cloud.analyze_complaint(
        translated_text, image_bytes=image_bytes, image_mime_type=image_mime_type
    )
    record = store_complaint(
        citizen_id, text, translated_text, source_lang,
        analysis, latitude, longitude, region,
    )

    return ComplaintResponse(**record)


# ---------------------------------------------------------------------------
# Endpoint: track a complaint with an opaque code handed back after submission.
# ---------------------------------------------------------------------------
@app.get("/complaints/track/{tracking_code}", response_model=ComplaintStatus)
def track_complaint(tracking_code: str):
    normalized_code = tracking_code.strip().upper()
    try:
        cloud_record = firebase.get_complaint(normalized_code)
    except Exception:
        cloud_record = None
    if cloud_record:
        return ComplaintStatus(**{
            key: cloud_record[key]
            for key in ("id", "tracking_code", "category", "status", "region", "created_at")
        })

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, tracking_code, category, status, region, created_at "
        "FROM complaints WHERE tracking_code = ?",
        (normalized_code,),
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="No complaint found with that ID")

    return ComplaintStatus(
        id=row[0], tracking_code=row[1], category=row[2], status=row[3],
        region=row[4], created_at=row[5],
    )


# ---------------------------------------------------------------------------
# Endpoint: update status — restricted to authenticated policymakers.
# ---------------------------------------------------------------------------
@app.patch("/complaints/{complaint_id}/status", response_model=ComplaintStatus)
async def update_complaint_status(
    complaint_id: int,
    status: str = Form(...),
    _username: str = Depends(require_auth),
):
    valid_statuses = {"submitted", "in_review", "resolved"}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of {valid_statuses}")

    try:
        cloud_record = next(
            (item for item in firebase.list_complaints() if int(item.get("id", -1)) == complaint_id),
            None,
        )
    except Exception:
        cloud_record = None

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE complaints SET status = ? WHERE id = ?", (status, complaint_id))
    conn.commit()
    row = conn.execute(
        "SELECT id, tracking_code, category, status, region, created_at "
        "FROM complaints WHERE id = ?",
        (complaint_id,),
    ).fetchone()
    conn.close()

    if row is None and cloud_record is None:
        raise HTTPException(status_code=404, detail="No complaint found with that ID")

    record = {
        "id": row[0], "tracking_code": row[1], "category": row[2],
        "status": row[3], "region": row[4], "created_at": row[5],
    } if row else {
        key: cloud_record[key]
        for key in ("id", "tracking_code", "category", "region", "created_at")
    }
    record["status"] = status
    try:
        firebase.update_complaint(record["tracking_code"], {"status": status})
    except Exception:
        pass
    publish_dashboard_event("refresh")
    return ComplaintStatus(**record)


class LocationResult(BaseModel):
    latitude: float
    longitude: float
    region: str
    display_name: str


def _region_from_address(address: dict, fallback: str) -> str:
    return (
        address.get("city") or address.get("town") or address.get("village")
        or address.get("municipality") or address.get("county")
        or address.get("state_district") or address.get("state") or fallback
    )


@app.get("/locations/geocode", response_model=LocationResult)
def geocode_location(query: str = Query(..., min_length=2, max_length=160)):
    try:
        google_result = google_cloud.geocode(query)
    except (requests.RequestException, ValueError):
        google_result = None
    if google_result:
        return LocationResult(**google_result)

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Location service is unavailable") from exc
    if not results:
        raise HTTPException(status_code=404, detail="Location not found")
    result = results[0]
    display_name = result.get("display_name", query)
    return LocationResult(
        latitude=float(result["lat"]), longitude=float(result["lon"]),
        region=_region_from_address(result.get("address", {}), display_name.split(",")[0]),
        display_name=display_name,
    )


@app.get("/locations/reverse", response_model=LocationResult)
def reverse_geocode_location(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
):
    try:
        google_result = google_cloud.reverse_geocode(latitude, longitude)
    except (requests.RequestException, ValueError):
        google_result = None
    if google_result:
        return LocationResult(**google_result)

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": latitude, "lon": longitude,
                "format": "jsonv2", "addressdetails": 1,
            },
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Location service is unavailable") from exc
    display_name = result.get("display_name", "Current location")
    return LocationResult(
        latitude=latitude, longitude=longitude,
        region=_region_from_address(result.get("address", {}), display_name.split(",")[0]),
        display_name=display_name,
    )


class ReasonEvidence(BaseModel):
    text: str
    reporter_count: int
    image_evidence: Optional[str] = None


class RegionPriority(BaseModel):
    region: str
    category: str
    complaint_count: int
    unique_reporter_count: int
    avg_severity: float
    latitude: float
    longitude: float
    category_weight: float
    demand_intensity: float
    urgency_factor: float
    investment_penalty: float
    investment_deduction: float
    priority_score: float
    escalated: bool = False
    escalated_to: Optional[str] = None
    escalated_at: Optional[str] = None
    infrastructure_index: Optional[float] = None
    current_investment_plan: str = ""
    representative_reasons: List[ReasonEvidence] = Field(default_factory=list)


def public_reason(text: str) -> str:
    """Remove obvious contact details before showing evidence publicly."""
    cleaned = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email removed]", text)
    cleaned = re.sub(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)", "[number removed]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:180] + ("…" if len(cleaned) > 180 else "")


def _sqlite_complaints() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT id, tracking_code, citizen_id, original_text, translated_text,
               source_lang, category, severity_score, latitude, longitude,
               region, status, created_at, ai_summary, infrastructure_need,
               responsible_department, ai_confidence, image_evidence, ai_provider
        FROM complaints
        """
    ).fetchall()
    conn.close()
    keys = (
        "id", "tracking_code", "citizen_id", "original_text", "translated_text",
        "source_lang", "category", "severity_score", "latitude", "longitude",
        "region", "status", "created_at", "ai_summary", "infrastructure_need",
        "responsible_department", "ai_confidence", "image_evidence", "ai_provider",
    )
    return [dict(zip(keys, row)) for row in rows]


def _sqlite_escalations() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT id, region, category, forwarded_to, note, status, created_at
        FROM escalations
        ORDER BY created_at DESC
        """
    ).fetchall()
    conn.close()
    keys = ("id", "region", "category", "forwarded_to", "note", "status", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def _priority_from_records(complaints: list, escalation_records: list) -> List[RegionPriority]:
    if not complaints:
        return []

    per_citizen = {}
    reason_groups = {}
    for complaint in complaints:
        region = complaint.get("region") or "Unknown"
        category = complaint.get("category") or "other"
        citizen_id = complaint.get("citizen_id") or complaint.get("tracking_code")
        citizen_key = (region, category, citizen_id)
        bucket = per_citizen.setdefault(
            citizen_key,
            {"count": 0, "severity": [], "latitudes": [], "longitudes": []},
        )
        bucket["count"] += 1
        bucket["severity"].append(float(complaint.get("severity_score") or 0.3))
        bucket["latitudes"].append(float(complaint.get("latitude") or 0))
        bucket["longitudes"].append(float(complaint.get("longitude") or 0))

        reason_text = complaint.get("ai_summary") or complaint.get("translated_text") or ""
        if reason_text.strip():
            reason_key = (region, category, reason_text.strip())
            evidence = reason_groups.setdefault(
                reason_key,
                {
                    "citizens": set(),
                    "latest": "",
                    "image_evidence": complaint.get("image_evidence"),
                },
            )
            evidence["citizens"].add(citizen_id)
            evidence["latest"] = max(evidence["latest"], complaint.get("created_at") or "")
            if not evidence.get("image_evidence") and complaint.get("image_evidence"):
                evidence["image_evidence"] = complaint["image_evidence"]

    group_rows = {}
    for (region, category, _citizen_id), values in per_citizen.items():
        group = group_rows.setdefault(
            (region, category),
            {"count": 0, "unique": 0, "severity": [], "latitudes": [], "longitudes": []},
        )
        group["count"] += values["count"]
        group["unique"] += 1
        group["severity"].append(sum(values["severity"]) / len(values["severity"]))
        group["latitudes"].append(sum(values["latitudes"]) / len(values["latitudes"]))
        group["longitudes"].append(sum(values["longitudes"]) / len(values["longitudes"]))

    latest_escalations = {}
    for escalation in sorted(
        escalation_records, key=lambda item: item.get("created_at", ""), reverse=True
    ):
        latest_escalations.setdefault(
            (escalation.get("region"), escalation.get("category")), escalation
        )

    reasons_by_group = {}
    for (region, category, text), evidence in reason_groups.items():
        reasons_by_group.setdefault((region, category), []).append(
            {
                "text": text,
                "reporter_count": len(evidence["citizens"]),
                "latest": evidence["latest"],
                "image_evidence": evidence.get("image_evidence"),
            }
        )
    for reasons in reasons_by_group.values():
        reasons.sort(key=lambda item: (item["reporter_count"], item["latest"]), reverse=True)

    contexts = bigquery_analytics.get_region_contexts(
        region for region, _category in group_rows.keys()
    )
    total_population = sum(
        contexts.get(region, {}).get(
            "population", scoring.REGION_POPULATION.get(region, scoring.DEFAULT_POPULATION)
        )
        for region, _category in group_rows.keys()
    )
    total_unique_reporters = sum(values["unique"] for values in group_rows.values())
    baseline_rate = total_unique_reporters / total_population if total_population else 0.0

    rates = []
    for (region, _category), values in group_rows.items():
        population = contexts.get(region, {}).get(
            "population", scoring.REGION_POPULATION.get(region, scoring.DEFAULT_POPULATION)
        )
        rates.append(scoring.smoothed_rate(values["unique"], population, baseline_rate))
    max_smoothed_rate = max(rates, default=0.0)
    max_log_reporters = max(
        (math.log1p(values["unique"]) for values in group_rows.values()), default=0.0
    )

    results = []
    for (region, category), values in group_rows.items():
        context = contexts.get(region, {})
        population = context.get(
            "population", scoring.REGION_POPULATION.get(region, scoring.DEFAULT_POPULATION)
        )
        investment_penalty = context.get(
            "investment_penalty",
            scoring.REGION_INVESTMENT_PENALTY.get(region, scoring.DEFAULT_INVESTMENT_PENALTY),
        )
        avg_severity = sum(values["severity"]) / len(values["severity"])
        breakdown = scoring.priority_breakdown(
            category, values["unique"], population, avg_severity, baseline_rate,
            max_smoothed_rate, max_log_reporters, investment_penalty,
        )
        escalation = latest_escalations.get((region, category))
        reasons = reasons_by_group.get((region, category), [])[:3]
        results.append(
            RegionPriority(
                region=region,
                category=category,
                complaint_count=values["count"],
                unique_reporter_count=values["unique"],
                avg_severity=round(avg_severity, 2),
                latitude=sum(values["latitudes"]) / len(values["latitudes"]),
                longitude=sum(values["longitudes"]) / len(values["longitudes"]),
                escalated=escalation is not None,
                escalated_to=escalation.get("forwarded_to") if escalation else None,
                escalated_at=escalation.get("created_at") if escalation else None,
                infrastructure_index=context.get("infrastructure_index"),
                current_investment_plan=context.get("current_investment_plan", ""),
                representative_reasons=[
                    ReasonEvidence(
                        text=public_reason(reason["text"]),
                        reporter_count=reason["reporter_count"],
                        image_evidence=public_reason(reason["image_evidence"])
                        if reason.get("image_evidence") else None,
                    )
                    for reason in reasons
                ],
                **breakdown,
            )
        )
    results.sort(key=lambda item: item.priority_score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Ranked priorities. Firebase is authoritative when connected; SQLite remains
# a zero-configuration fallback. BigQuery supplies demographic/investment
# context to the same explainable scoring method.
# ---------------------------------------------------------------------------
@app.get("/regions/priority", response_model=List[RegionPriority])
def get_region_priorities(_username: str = Depends(require_auth)):
    try:
        complaints = firebase.list_complaints()
    except Exception:
        complaints = []
    if not complaints:
        complaints = _sqlite_complaints()

    try:
        escalations = firebase.list_escalations()
    except Exception:
        escalations = []
    if not escalations:
        escalations = _sqlite_escalations()
    return _priority_from_records(complaints, escalations)


class EscalationResponse(BaseModel):
    id: int
    region: str
    category: str
    forwarded_to: str
    note: str
    status: str
    created_at: str


# ---------------------------------------------------------------------------
# Endpoint: forward a region/category's complaints to the responsible
# government body. Auto-routes by category (scoring.ESCALATION_TARGETS)
# rather than making the policymaker pick a recipient manually, and logs
# a real, timestamped record — not just a UI state change.
# ---------------------------------------------------------------------------
@app.post("/regions/escalate", response_model=EscalationResponse)
async def escalate_region(
    region: str = Form(...),
    category: str = Form(...),
    note: str = Form(""),
    _username: str = Depends(require_auth),
):
    forwarded_to = scoring.ESCALATION_TARGETS.get(category, scoring.ESCALATION_TARGETS["other"])
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        allocated_id = firebase.allocate_id("escalations")
    except Exception:
        allocated_id = None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """
        INSERT INTO escalations (id, region, category, forwarded_to, note, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'forwarded', ?)
        """,
        (allocated_id, region, category, forwarded_to, note, created_at),
    )
    conn.execute(
        "UPDATE complaints SET status = 'in_review' "
        "WHERE region = ? AND category = ? AND status = 'submitted'",
        (region, category),
    )
    conn.commit()
    escalation_id = cur.lastrowid
    conn.close()
    record = {
        "id": escalation_id,
        "region": region,
        "category": category,
        "forwarded_to": forwarded_to,
        "note": note,
        "status": "forwarded",
        "created_at": created_at,
    }
    try:
        firebase.save_escalation(record)
        firebase.update_group_status(region, category, "in_review")
    except Exception:
        pass
    publish_dashboard_event("refresh")

    return EscalationResponse(**record)


# ---------------------------------------------------------------------------
# Endpoint: the escalation log — every forward ever made, most recent
# first. This is the policymaker-side counterpart to the citizen's
# /complaints/{id} tracker: "did it actually reach them, what's the status."
# ---------------------------------------------------------------------------
@app.get("/escalations", response_model=List[EscalationResponse])
def list_escalations(_username: str = Depends(require_auth)):
    try:
        records = firebase.list_escalations()
    except Exception:
        records = []
    if not records:
        records = _sqlite_escalations()
    return [EscalationResponse(**record) for record in records]


# ---------------------------------------------------------------------------
# Endpoint: update an escalation's status (forwarded -> acknowledged ->
# resolved). Manually driven for the prototype — a real deployment would
# have this update via the receiving department's own system instead.
# ---------------------------------------------------------------------------
@app.patch("/escalations/{escalation_id}/status", response_model=EscalationResponse)
async def update_escalation_status(
    escalation_id: int, status: str = Form(...), _username: str = Depends(require_auth)
):
    valid_statuses = {"forwarded", "acknowledged", "resolved"}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of {valid_statuses}")

    try:
        cloud_record = next(
            (item for item in firebase.list_escalations() if int(item.get("id", -1)) == escalation_id),
            None,
        )
    except Exception:
        cloud_record = None

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, region, category, forwarded_to, note, status, created_at FROM escalations WHERE id = ?",
        (escalation_id,),
    ).fetchone()
    if row is None and cloud_record is None:
        conn.close()
        raise HTTPException(status_code=404, detail="No escalation found with that ID")

    region = row[1] if row else cloud_record["region"]
    category = row[2] if row else cloud_record["category"]
    if row is not None:
        conn.execute("UPDATE escalations SET status = ? WHERE id = ?", (status, escalation_id))
    complaint_status = "resolved" if status == "resolved" else "in_review"
    conn.execute(
        "UPDATE complaints SET status = ? WHERE region = ? AND category = ?",
        (complaint_status, region, category),
    )
    conn.commit()
    if row is not None:
        row = conn.execute(
            "SELECT id, region, category, forwarded_to, note, status, created_at "
            "FROM escalations WHERE id = ?",
            (escalation_id,),
        ).fetchone()
    conn.close()
    try:
        firebase.update_escalation(escalation_id, {"status": status})
        firebase.update_group_status(region, category, complaint_status)
    except Exception:
        pass
    publish_dashboard_event("refresh")

    if row is not None:
        return EscalationResponse(
            id=row[0], region=row[1], category=row[2], forwarded_to=row[3],
            note=row[4] or "", status=row[5], created_at=row[6],
        )
    cloud_record["status"] = status
    cloud_record["note"] = cloud_record.get("note") or ""
    return EscalationResponse(**cloud_record)


class ForecastResponse(BaseModel):
    horizon_days: int
    model: str
    forecasts: List[dict]
    status: str


@app.get("/analytics/forecast", response_model=ForecastResponse)
def forecast_hotspots(
    horizon_days: int = Query(30, ge=7, le=90),
    _username: str = Depends(require_auth),
):
    forecasts = bigquery_analytics.forecast_hotspots(horizon_days)
    return ForecastResponse(
        horizon_days=horizon_days,
        model="BigQuery ML ARIMA_PLUS",
        forecasts=forecasts,
        status="ready" if forecasts else "collecting_history",
    )


@app.post("/regions/policy-brief", response_model=PolicyBrief)
def create_policy_brief(
    region: str = Form(...),
    category: str = Form(...),
    _username: str = Depends(require_auth),
):
    priorities = get_region_priorities(_username)
    priority = next(
        (item for item in priorities if item.region == region and item.category == category),
        None,
    )
    if priority is None:
        raise HTTPException(status_code=404, detail="Priority group not found")
    context = priority.model_dump() if hasattr(priority, "model_dump") else priority.dict()
    context["forecast"] = bigquery_analytics.forecast_hotspots(30)[:10]
    try:
        return google_cloud.generate_policy_brief(context)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Gemini policy brief is unavailable") from exc


@app.get("/config/public")
def public_config():
    firebase_config = firebase.public_config()
    return {
        "firebase": firebase_config,
        "google_maps_api_key": google_cloud.maps_browser_api_key,
        "google_services": google_cloud.capability_status(),
        "bigquery": {
            "configured": bool(bigquery_analytics.project_id),
            "dataset": bigquery_analytics.dataset,
            "forecast_model": "BigQuery ML ARIMA_PLUS",
        },
    }


async def dashboard_event_stream():
    queue = asyncio.Queue(maxsize=1)
    DASHBOARD_SUBSCRIBERS.add(queue)
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                event_type = await asyncio.wait_for(queue.get(), timeout=20)
                yield f"event: {event_type}\ndata: refresh\n\n"
            except asyncio.TimeoutError:
                # Comment frames keep proxies from closing an otherwise idle
                # connection without triggering a browser event.
                yield ": keepalive\n\n"
    finally:
        DASHBOARD_SUBSCRIBERS.discard(queue)


@app.get("/events/dashboard")
async def dashboard_events():
    return StreamingResponse(
        dashboard_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/")
def root():
    return {"status": "Citizen Demand Intake API is running"}


# Serves static/index.html at /dashboard — the policymaker dashboard.
# Mounted last so it doesn't shadow any API route above.
app.mount("/dashboard", StaticFiles(directory="static", html=True), name="dashboard")
app.mount("/citizen", StaticFiles(directory="citizen", html=True), name="citizen")
