import os
import re
import json
import math
import time
import secrets
import hashlib
import hmac
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Set
from urllib.parse import unquote, parse_qs

from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from ortools.sat.python import cp_model

# ==============================================================================
# Application & Database Configuration
# ==============================================================================
app = FastAPI(title="Smart Timetable & Resource Optimizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "null",
        "*"
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def resolve_db_path() -> Path:
    env_path = os.environ.get("TIMETABLE_DB") or os.environ.get("DATABASE_PATH")
    if env_path:
        p = Path(env_path).resolve()
        if p.exists() or p.parent.exists():
            return p
    script_db = Path(__file__).resolve().parent / "timetable.db"
    if script_db.exists():
        return script_db
    cwd_db = Path.cwd() / "timetable.db"
    if cwd_db.exists():
        return cwd_db
    return script_db

DB_PATH = resolve_db_path()
BASE_DIR = Path(__file__).resolve().parent
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
VALID_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
VALID_DAYS_MAP = {d.lower(): d for d in VALID_DAYS}

def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ==============================================================================
# Security, Passwords & Authentication State
# ==============================================================================
def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex(), salt

def verify_password(password: str, salt: str, password_hash: str) -> bool:
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return hmac.compare_digest(key.hex(), password_hash)

ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_DURATION_SECONDS = 24 * 3600

def create_session(username: str, role: str = "admin") -> str:
    token = secrets.token_urlsafe(32)
    ACTIVE_SESSIONS[token] = {
        "username": username,
        "role": role,
        "expires_at": time.time() + SESSION_DURATION_SECONDS
    }
    return token

security = HTTPBearer(auto_error=False)

def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Dict[str, Any]:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials.strip()
    sess = ACTIVE_SESSIONS.get(token)
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if time.time() > sess["expires_at"]:
        ACTIVE_SESSIONS.pop(token, None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return sess

# ==============================================================================
# Availability & Equipment Validation Helpers
# ==============================================================================
def parse_and_validate_availability(value: str, entity_name: str = "Resource") -> List[str]:
    if not value or not str(value).strip():
        raise HTTPException(
            status_code=400,
            detail=f"{entity_name} availability cannot be empty. Must include at least one valid day from: {', '.join(VALID_DAYS)}"
        )
    raw_tokens = [x.strip() for x in str(value).replace(";", ",").split(",") if x.strip()]
    valid_days = []
    invalid_tokens = []
    for tok in raw_tokens:
        low = tok.lower()
        if low in VALID_DAYS_MAP:
            day_canon = VALID_DAYS_MAP[low]
            if day_canon not in valid_days:
                valid_days.append(day_canon)
        else:
            invalid_tokens.append(tok)
    
    if invalid_tokens:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid availability day(s) for {entity_name}: '{', '.join(invalid_tokens)}'. Allowed days are: {', '.join(VALID_DAYS)}"
        )
    if not valid_days:
        raise HTTPException(
            status_code=400,
            detail=f"{entity_name} availability must include at least one valid day from: {', '.join(VALID_DAYS)}"
        )
    return valid_days

def days_from_text(value: str) -> Set[str]:
    if not value or not str(value).strip():
        return set()
    raw_tokens = [x.strip().lower() for x in str(value).replace(";", ",").split(",") if x.strip()]
    return {VALID_DAYS_MAP[x] for x in raw_tokens if x in VALID_DAYS_MAP}

NO_EQUIPMENT_VALUES = {
    "", "none", "n/a", "na", "not required", "not-required", "no equipment",
    "no equipment required", "not applicable", "nil", "-", "any", "all", "null"
}

def is_no_equipment(val: Optional[str]) -> bool:
    if not val:
        return True
    s = str(val).strip().lower()
    if s in NO_EQUIPMENT_VALUES:
        return True
    clean = re.sub(r'[\s\.\-_/]+', '', s)
    return clean in {"", "none", "na", "notrequired", "notapplicable", "nil", "any", "all", "null"}

def is_equipment_compatible(required: Optional[str], available: Optional[str], lab_id: Optional[str] = "") -> bool:
    if is_no_equipment(required):
        return True
    req_tokens = [tok.strip().lower() for tok in re.split(r'[,;+&/]+', str(required)) if tok.strip()]
    meaningful_tokens = [tok for tok in req_tokens if not is_no_equipment(tok)]
    if not meaningful_tokens:
        return True
    avail_str = f"{available or ''} {lab_id or ''}".lower()
    return all(tok in avail_str for tok in meaningful_tokens)


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS teachers (
        name TEXT PRIMARY KEY,
        subject TEXT,
        workload INTEGER,
        availability TEXT
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rooms (
        id TEXT PRIMARY KEY,
        capacity INTEGER,
        location TEXT,
        availability TEXT
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS labs (
        id TEXT PRIMARY KEY,
        capacity INTEGER,
        equipment TEXT,
        availability TEXT
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sections (
        name TEXT PRIMARY KEY,
        year TEXT,
        branch TEXT,
        section TEXT,
        strength INTEGER
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        code TEXT PRIMARY KEY,
        name TEXT,
        teacher TEXT,
        section TEXT,
        type TEXT,
        classes_per_week INTEGER
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS timeslots (
        time TEXT PRIMARY KEY,
        type TEXT,
        order_index INTEGER
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS timetable_state (
        id INTEGER PRIMARY KEY,
        json_data TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password_hash TEXT,
        salt TEXT,
        role TEXT DEFAULT 'admin'
    )""")

    conn.commit()

    # Dynamic migrations for existing databases
    cursor.execute("PRAGMA table_info(sections)")
    sec_cols = [r["name"] for r in cursor.fetchall()]
    if "year" not in sec_cols:
        cursor.execute("ALTER TABLE sections ADD COLUMN year TEXT DEFAULT '3rd Year'")
    if "branch" not in sec_cols:
        cursor.execute("ALTER TABLE sections ADD COLUMN branch TEXT DEFAULT 'Computer Science'")
    if "section" not in sec_cols:
        cursor.execute("ALTER TABLE sections ADD COLUMN section TEXT DEFAULT 'A'")
    if "strength" not in sec_cols:
        cursor.execute("ALTER TABLE sections ADD COLUMN strength INTEGER DEFAULT 50")

    cursor.execute("PRAGMA table_info(timeslots)")
    ts_cols = [r["name"] for r in cursor.fetchall()]
    if ts_cols:
        if "type" not in ts_cols:
            cursor.execute("ALTER TABLE timeslots ADD COLUMN type TEXT DEFAULT 'Teaching'")
        if "order_index" not in ts_cols:
            cursor.execute("ALTER TABLE timeslots ADD COLUMN order_index INTEGER DEFAULT 1")

    cursor.execute("PRAGMA table_info(subjects)")
    sub_cols = [r["name"] for r in cursor.fetchall()]
    if "required_equipment" not in sub_cols:
        cursor.execute("ALTER TABLE subjects ADD COLUMN required_equipment TEXT DEFAULT ''")
    if "duration" not in sub_cols:
        cursor.execute("ALTER TABLE subjects ADD COLUMN duration INTEGER DEFAULT 1")
    if "preferred_day" not in sub_cols:
        cursor.execute("ALTER TABLE subjects ADD COLUMN preferred_day TEXT DEFAULT ''")
    if "preferred_period" not in sub_cols:
        cursor.execute("ALTER TABLE subjects ADD COLUMN preferred_period TEXT DEFAULT ''")
    if "availability" not in sub_cols:
        cursor.execute("ALTER TABLE subjects ADD COLUMN availability TEXT DEFAULT ''")

    # Ensure dedicated Central Library room exists
    cursor.execute("SELECT id FROM rooms WHERE LOWER(id) LIKE '%lib%'")
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO rooms (id, capacity, location, availability) VALUES (?, ?, ?, ?)",
            ("Central Library", 100, "Library Building", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday")
        )

    # Ensure any legacy subjects with duration > 1 have classes_per_week >= duration
    cursor.execute("UPDATE subjects SET classes_per_week = duration WHERE duration > 1 AND classes_per_week < duration")

    conn.commit()

    # Ensure institutional admin user exists and password verifies with password123
    cursor.execute("SELECT username, password_hash, salt FROM users WHERE LOWER(username) = 'admin@college.edu'")
    admin_row = cursor.fetchone()
    if not admin_row or not verify_password("password123", admin_row["salt"], admin_row["password_hash"]):
        h, s = hash_password("password123")
        cursor.execute(
            "INSERT OR REPLACE INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
            ("admin@college.edu", h, s, "admin")
        )
        conn.commit()

    # Seed initial institutional data if empty
    cursor.execute("SELECT COUNT(*) AS cnt FROM teachers")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany(
            "INSERT INTO teachers (name, subject, workload, availability) VALUES (?, ?, ?, ?)",
            [
                ("Dr. Smith", "Data Structures", 2, "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"),
                ("Prof. Johnson", "Operating Systems", 2, "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"),
                ("Mr. Davis", "Computer Networks", 2, "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"),
            ]
        )

    cursor.execute("SELECT COUNT(*) AS cnt FROM rooms")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany(
            "INSERT INTO rooms (id, capacity, location, availability) VALUES (?, ?, ?, ?)",
            [
                ("Room 101", 60, "Main Block", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"),
                ("Room 102", 45, "Main Block", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"),
            ]
        )

    cursor.execute("SELECT COUNT(*) AS cnt FROM labs")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany(
            "INSERT INTO labs (id, capacity, equipment, availability) VALUES (?, ?, ?, ?)",
            [
                ("CS Lab 1", 60, "60 PCs, Linux OS", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"),
            ]
        )

    cursor.execute("SELECT COUNT(*) AS cnt FROM sections")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany(
            "INSERT INTO sections (name, year, branch, section, strength) VALUES (?, ?, ?, ?, ?)",
            [
                ("CSE-3A", "3rd Year", "Computer Science", "A", 50),
                ("CSE-3B", "3rd Year", "Computer Science", "B", 40),
            ]
        )

    cursor.execute("SELECT COUNT(*) AS cnt FROM subjects")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany(
            "INSERT INTO subjects (code, name, teacher, section, type, classes_per_week) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("DS", "Data Structures", "Dr. Smith", "CSE-3A", "Lecture", 2),
                ("OS", "Operating Systems", "Prof. Johnson", "CSE-3A", "Lecture", 2),
                ("CN", "Computer Networks", "Mr. Davis", "CSE-3B", "Lecture", 2),
            ]
        )

    cursor.execute("SELECT COUNT(*) AS cnt FROM timeslots")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany(
            "INSERT INTO timeslots (time, type, order_index) VALUES (?, ?, ?)",
            [
                ("09:00 - 10:00", "Teaching", 1),
                ("10:00 - 11:00", "Teaching", 2),
                ("11:00 - 12:00", "Teaching", 3),
                ("12:00 - 13:00", "Break", 4),
                ("13:00 - 14:00", "Teaching", 5),
                ("14:00 - 15:00", "Teaching", 6),
                ("15:00 - 16:00", "Teaching", 7),
            ]
        )

    conn.commit()
    conn.close()

# Initialize SQLite database immediately upon startup
init_db()

# ==============================================================================
# Pydantic Request & Response Models
# ==============================================================================
class Login(BaseModel):
    username: str
    password: str

class Teacher(BaseModel):
    name: str
    subject: str
    workload: int = Field(default=2, ge=0, le=100)
    availability: str = "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"

class Room(BaseModel):
    id: str
    capacity: int = Field(default=60, ge=1)
    location: str = "Main Block"
    availability: str = "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"

class Lab(BaseModel):
    id: str
    capacity: int = Field(default=60, ge=1)
    equipment: str = "Computers & Network Equipment"
    availability: str = "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"

class Section(BaseModel):
    name: str
    year: str = "3rd Year"
    branch: str = "Computer Science"
    section: str = "A"
    strength: int = Field(default=50, ge=0)

class Subject(BaseModel):
    code: str
    name: str
    teacher: str
    section: str
    type: str = "Classroom"
    classes_per_week: int = Field(default=2, ge=0, le=50)
    required_equipment: Optional[str] = ""
    duration: int = Field(default=1, ge=0, le=20)
    preferred_day: Optional[str] = ""
    preferred_period: Optional[str] = ""
    availability: Optional[str] = ""

class TimeSlot(BaseModel):
    time: str
    type: str = "Teaching"
    order_index: int = 1

class Weights(BaseModel):
    morning_preference: int = Field(default=3, ge=0, le=5)
    minimize_gaps: int = Field(default=2, ge=0, le=5)
    balance_workload: int = Field(default=2, ge=0, le=5)

class Session(BaseModel):
    subject: str
    teacher: str
    room: str
    section: str = ""
    branch: str = ""
    year: str = ""
    section_letter: str = ""
    duration: int = 1
    session_type: str = "Classroom"

class Slot(BaseModel):
    time: str
    subject: str
    teacher: str
    room: str
    section: str = ""
    slot_type: str = "Teaching"
    sessions: List[Session] = []

class Day(BaseModel):
    day: str
    slots: List[Slot]

class Result(BaseModel):
    status: str
    message: str
    solver_status: Optional[str] = None
    timetable: Optional[List[Day]] = None
    section_timetables: Optional[Dict[str, List[Day]]] = None
    sections_info: Optional[List[Section]] = None
    conflicts: Optional[str] = None
    validation_report: Optional[List[Dict[str, Any]]] = None

# ==============================================================================
# REST API Endpoints & Static Files
# ==============================================================================
@app.get("/")
@app.get("/index.html")
def serve_index():
    index_file = BASE_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    return {"message": "Smart Timetable API is running", "status": "online"}

@app.get("/style.css")
def serve_css():
    css_file = BASE_DIR / "style.css"
    if css_file.exists():
        return FileResponse(str(css_file), media_type="text/css")
    raise HTTPException(status_code=404, detail="style.css not found")

@app.get("/script.js")
def serve_js():
    js_file = BASE_DIR / "script.js"
    if js_file.exists():
        return FileResponse(str(js_file), media_type="application/javascript")
    raise HTTPException(status_code=404, detail="script.js not found")

@app.get("/api")
@app.get("/api/")
@app.get("/api/health")
@app.get("/health")
def health_check():
    return {"message": "Smart Timetable API is running", "status": "online", "database": "sqlite"}

@app.get("/api/login")
@app.get("/login")
def login_get_info():
    return {
        "status": "online",
        "message": "Login endpoint accepts POST requests with 'username' and 'password'",
        "method": "POST",
        "default_credentials": {
            "username": "admin@college.edu",
            "password": "password123"
        }
    }

@app.post("/api/login")
@app.post("/login")
async def login(request: Request):
    content_type = request.headers.get("content-type", "")
    u = ""
    p = ""
    
    if "application/x-www-form-urlencoded" in content_type:
        raw_body = await request.body()
        parsed = parse_qs(raw_body.decode("utf-8"))
        u = parsed.get("username", [""])[0].strip().lower()
        p = parsed.get("password", [""])[0].strip()
    else:
        try:
            data = await request.json()
            u = str(data.get("username", "")).strip().lower()
            p = str(data.get("password", "")).strip()
        except Exception:
            raw_body = await request.body()
            parsed = parse_qs(raw_body.decode("utf-8"))
            u = parsed.get("username", [""])[0].strip().lower()
            p = parsed.get("password", [""])[0].strip()

    if not u or not p:
        raise HTTPException(status_code=400, detail="Username and password are required")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username, password_hash, salt, role FROM users WHERE LOWER(username) = ?", (u,))
    user_row = cursor.fetchone()
    conn.close()

    if not user_row:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not verify_password(p, user_row["salt"], user_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_session(user_row["username"], user_row["role"] or "admin")
    return {
        "status": "success",
        "message": "Login successful",
        "token": token,
        "access_token": token,
        "token_type": "bearer",
        "username": user_row["username"],
        "role": user_row["role"] or "admin"
    }

@app.get("/api/auth/verify")
@app.get("/auth/verify")
def verify_session(current_user: Dict[str, Any] = Depends(get_current_user)):
    return {
        "status": "authenticated",
        "username": current_user["username"],
        "role": current_user["role"]
    }

@app.post("/api/logout")
@app.post("/logout")
def logout(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if credentials and credentials.credentials:
        ACTIVE_SESSIONS.pop(credentials.credentials.strip(), None)
    return {"status": "success", "message": "Logged out successfully"}

# --- STATE INVALIDATION HELPER ---
def invalidate_timetable_state(cursor):
    """Invalidates stored timetable in SQLite whenever any institutional data changes."""
    cursor.execute("DELETE FROM timetable_state WHERE id = 1")

# --- TEACHERS CRUD ---
@app.get("/api/teachers", response_model=List[Teacher])
def get_teachers(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name, subject, workload, availability FROM teachers ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    return [Teacher(**dict(r)) for r in rows]

@app.get("/api/teachers/{teacher_name}", response_model=Teacher)
def get_teacher(teacher_name: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(teacher_name)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name, subject, workload, availability FROM teachers WHERE LOWER(name) = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Teacher '{teacher_name}' not found")
    return Teacher(**dict(row))

@app.post("/api/teachers", response_model=Teacher)
def add_teacher(x: Teacher, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_name = unquote(x.name).strip()
    valid_days = parse_and_validate_availability(x.availability, "Teacher")
    clean_avail = ",".join(valid_days)
    x.availability = clean_avail

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM teachers WHERE LOWER(name) = ?", (clean_name.lower(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Teacher with this name already exists")
    cursor.execute(
        "INSERT INTO teachers (name, subject, workload, availability) VALUES (?, ?, ?, ?)",
        (clean_name, x.subject.strip(), x.workload, clean_avail)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.put("/api/teachers/{teacher_name}", response_model=Teacher)
@app.patch("/api/teachers/{teacher_name}", response_model=Teacher)
def update_teacher(teacher_name: str, x: Teacher, current_user: Dict[str, Any] = Depends(get_current_user)):
    old_clean = unquote(str(teacher_name)).strip().lower()
    new_clean = unquote(x.name).strip()
    valid_days = parse_and_validate_availability(x.availability, "Teacher")
    clean_avail = ",".join(valid_days)
    x.availability = clean_avail

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM teachers WHERE LOWER(name) = ?", (old_clean,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"Teacher '{teacher_name}' not found")
    
    if new_clean.lower() != old_clean:
        cursor.execute("SELECT name FROM teachers WHERE LOWER(name) = ?", (new_clean.lower(),))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Another teacher with this name already exists")
    # Keep workload as a hard constraint: prevent reducing cap below currently assigned classes
    cursor.execute(
        "SELECT COALESCE(SUM(classes_per_week), 0) AS total_assigned FROM subjects WHERE LOWER(TRIM(teacher)) = LOWER(TRIM(?))",
        (old_clean,)
    )
    assigned_total = int(cursor.fetchone()["total_assigned"])
    if x.workload < assigned_total:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=(
                f"Teacher '{new_clean}' workload cap cannot be reduced to {x.workload} classes/week: "
                f"Currently assigned total is {assigned_total} classes/week across subjects. "
                f"Maximum capacity must be at least {assigned_total} classes/week."
            )
        )

    cursor.execute(
        "UPDATE teachers SET name = ?, subject = ?, workload = ?, availability = ? WHERE LOWER(name) = ?",
        (new_clean, x.subject.strip(), x.workload, clean_avail, old_clean)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.delete("/api/teachers/{teacher_name}")
def delete_teacher(teacher_name: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(teacher_name)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM teachers WHERE LOWER(name) = ?", (clean,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Teacher '{teacher_name}' not found")
    actual_name = row["name"]
    cursor.execute("DELETE FROM subjects WHERE LOWER(teacher) = ?", (clean,))
    cursor.execute("DELETE FROM teachers WHERE LOWER(name) = ?", (clean,))
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Teacher '{actual_name}' deleted successfully"}

# --- ROOMS CRUD ---
@app.get("/api/rooms", response_model=List[Room])
def get_rooms(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, capacity, location, availability FROM rooms ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    return [Room(**dict(r)) for r in rows]

@app.get("/api/rooms/{room_id}", response_model=Room)
def get_room(room_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(room_id)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, capacity, location, availability FROM rooms WHERE LOWER(id) = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Room '{room_id}' not found")
    return Room(**dict(row))

@app.post("/api/rooms", response_model=Room)
def add_room(x: Room, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_id = unquote(x.id).strip()
    valid_days = parse_and_validate_availability(x.availability, "Room")
    clean_avail = ",".join(valid_days)
    x.availability = clean_avail

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM rooms WHERE LOWER(id) = ?", (clean_id.lower(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Room with this ID already exists")
    cursor.execute(
        "INSERT INTO rooms (id, capacity, location, availability) VALUES (?, ?, ?, ?)",
        (clean_id, x.capacity, x.location.strip(), clean_avail)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.put("/api/rooms/{room_id}", response_model=Room)
@app.patch("/api/rooms/{room_id}", response_model=Room)
def update_room(room_id: str, x: Room, current_user: Dict[str, Any] = Depends(get_current_user)):
    old_clean = unquote(str(room_id)).strip().lower()
    new_clean = unquote(x.id).strip()
    valid_days = parse_and_validate_availability(x.availability, "Room")
    clean_avail = ",".join(valid_days)
    x.availability = clean_avail

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM rooms WHERE LOWER(id) = ?", (old_clean,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"Room '{room_id}' not found")

    if new_clean.lower() != old_clean:
        cursor.execute("SELECT id FROM rooms WHERE LOWER(id) = ?", (new_clean.lower(),))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Another room with this ID already exists")

    cursor.execute(
        "UPDATE rooms SET id = ?, capacity = ?, location = ?, availability = ? WHERE LOWER(id) = ?",
        (new_clean, x.capacity, x.location.strip(), clean_avail, old_clean)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.delete("/api/rooms/{room_id}")
def delete_room(room_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(room_id)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM rooms WHERE LOWER(id) = ?", (clean,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Room '{room_id}' not found")
    actual_id = row["id"]
    cursor.execute("DELETE FROM rooms WHERE LOWER(id) = ?", (clean,))
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Room '{actual_id}' deleted successfully"}

# --- LABS CRUD ---
@app.get("/api/labs", response_model=List[Lab])
def get_labs(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, capacity, equipment, availability FROM labs ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    return [Lab(**dict(r)) for r in rows]

@app.get("/api/labs/{lab_id}", response_model=Lab)
def get_lab(lab_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(lab_id)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, capacity, equipment, availability FROM labs WHERE LOWER(id) = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Lab '{lab_id}' not found")
    return Lab(**dict(row))

@app.post("/api/labs", response_model=Lab)
def add_lab(x: Lab, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_id = unquote(x.id).strip()
    valid_days = parse_and_validate_availability(x.availability, "Lab")
    clean_avail = ",".join(valid_days)
    x.availability = clean_avail

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM labs WHERE LOWER(id) = ?", (clean_id.lower(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Lab with this ID already exists")
    cursor.execute(
        "INSERT INTO labs (id, capacity, equipment, availability) VALUES (?, ?, ?, ?)",
        (clean_id, x.capacity, x.equipment.strip(), clean_avail)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.put("/api/labs/{lab_id}", response_model=Lab)
@app.patch("/api/labs/{lab_id}", response_model=Lab)
def update_lab(lab_id: str, x: Lab, current_user: Dict[str, Any] = Depends(get_current_user)):
    old_clean = unquote(str(lab_id)).strip().lower()
    new_clean = unquote(x.id).strip()
    valid_days = parse_and_validate_availability(x.availability, "Lab")
    clean_avail = ",".join(valid_days)
    x.availability = clean_avail

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM labs WHERE LOWER(id) = ?", (old_clean,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"Lab '{lab_id}' not found")

    if new_clean.lower() != old_clean:
        cursor.execute("SELECT id FROM labs WHERE LOWER(id) = ?", (new_clean.lower(),))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Another lab with this ID already exists")

    cursor.execute(
        "UPDATE labs SET id = ?, capacity = ?, equipment = ?, availability = ? WHERE LOWER(id) = ?",
        (new_clean, x.capacity, x.equipment.strip(), clean_avail, old_clean)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.delete("/api/labs/{lab_id}")
def delete_lab(lab_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(lab_id)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM labs WHERE LOWER(id) = ?", (clean,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Lab '{lab_id}' not found")
    actual_id = row["id"]
    cursor.execute("DELETE FROM labs WHERE LOWER(id) = ?", (clean,))
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Lab '{actual_id}' deleted successfully"}

# --- SECTIONS / CLASSES CRUD (UNLIMITED) ---
@app.get("/api/sections", response_model=List[Section])
def get_sections(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name, year, branch, section, strength FROM sections ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    return [Section(**dict(r)) for r in rows]

@app.get("/api/sections/{section_name}", response_model=Section)
def get_section(section_name: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(section_name)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name, year, branch, section, strength FROM sections WHERE LOWER(name) = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Section '{section_name}' not found")
    return Section(**dict(row))

@app.post("/api/sections", response_model=Section)
def add_section(x: Section, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_name = unquote(x.name).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sections WHERE LOWER(name) = ?", (clean_name.lower(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Section with this name already exists")
    cursor.execute(
        "INSERT INTO sections (name, year, branch, section, strength) VALUES (?, ?, ?, ?, ?)",
        (clean_name, x.year.strip(), x.branch.strip(), x.section.strip(), x.strength)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.put("/api/sections/{section_name}", response_model=Section)
@app.patch("/api/sections/{section_name}", response_model=Section)
def update_section(section_name: str, x: Section, current_user: Dict[str, Any] = Depends(get_current_user)):
    old_clean = unquote(str(section_name)).strip().lower()
    new_clean = unquote(x.name).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sections WHERE LOWER(name) = ?", (old_clean,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"Section '{section_name}' not found")

    if new_clean.lower() != old_clean:
        cursor.execute("SELECT name FROM sections WHERE LOWER(name) = ?", (new_clean.lower(),))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Another section with this name already exists")
        cursor.execute("UPDATE subjects SET section = ? WHERE LOWER(section) = ?", (new_clean, old_clean))

    cursor.execute(
        "UPDATE sections SET name = ?, year = ?, branch = ?, section = ?, strength = ? WHERE LOWER(name) = ?",
        (new_clean, x.year.strip(), x.branch.strip(), x.section.strip(), x.strength, old_clean)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.delete("/api/sections/{section_name}")
def delete_section(section_name: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(section_name)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sections WHERE LOWER(name) = ?", (clean,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Section '{section_name}' not found")
    actual_name = row["name"]
    cursor.execute("DELETE FROM subjects WHERE LOWER(section) = ?", (clean,))
    cursor.execute("DELETE FROM sections WHERE LOWER(name) = ?", (clean,))
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Section '{actual_name}' deleted successfully"}

# --- SUBJECTS CRUD ---
@app.get("/api/subjects", response_model=List[Subject])
def get_subjects(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT code, name, teacher, section, type, classes_per_week, required_equipment, COALESCE(duration, 1) AS duration, COALESCE(preferred_day, '') AS preferred_day, COALESCE(preferred_period, '') AS preferred_period, COALESCE(availability, '') AS availability FROM subjects ORDER BY code")
    rows = cursor.fetchall()
    conn.close()
    return [Subject(**dict(r)) for r in rows]

@app.get("/api/subjects/{code}", response_model=Subject)
def get_subject(code: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(code)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT code, name, teacher, section, type, classes_per_week, required_equipment, COALESCE(duration, 1) AS duration, COALESCE(preferred_day, '') AS preferred_day, COALESCE(preferred_period, '') AS preferred_period, COALESCE(availability, '') AS availability FROM subjects WHERE LOWER(code) = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Subject '{code}' not found")
    return Subject(**dict(row))

@app.post("/api/subjects", response_model=Subject)
def add_subject(x: Subject, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_code = unquote(x.code).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM subjects WHERE LOWER(code) = ?", (clean_code.lower(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Subject with this code already exists")
    
    # Validate referenced teacher and enforce workload capacity hard constraint
    clean_teacher = unquote(x.teacher).strip()
    cursor.execute("SELECT name, workload FROM teachers WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))", (clean_teacher,))
    teacher_row = cursor.fetchone()
    if not teacher_row:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Assigned teacher '{x.teacher}' does not exist in Teachers database")

    canonical_teacher_name = teacher_row["name"]
    teacher_workload_cap = int(teacher_row["workload"])

    cursor.execute(
        "SELECT COALESCE(SUM(classes_per_week), 0) AS total_assigned FROM subjects WHERE LOWER(TRIM(teacher)) = LOWER(TRIM(?))",
        (canonical_teacher_name,)
    )
    current_assigned = int(cursor.fetchone()["total_assigned"])
    new_total = current_assigned + x.classes_per_week

    if new_total > teacher_workload_cap:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=(
                f"Teacher '{canonical_teacher_name}' workload cap exceeded: "
                f"Assigned total would be {new_total} classes/week (currently assigned: {current_assigned}, requested: {x.classes_per_week}), "
                f"which exceeds the maximum capacity of {teacher_workload_cap} classes/week."
            )
        )
    
    cursor.execute("SELECT name FROM sections WHERE LOWER(name) = ?", (unquote(x.section).strip().lower(),))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"Assigned section '{x.section}' does not exist in Sections database")

    req_eq = (x.required_equipment or "").strip()
    dur = max(1, getattr(x, "duration", 1) or 1)
    if dur > 1 and x.classes_per_week < dur:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Subject '{x.name}' has duration {dur} periods, so Periods / Week must be at least {dur}."
        )
    p_day = (getattr(x, "preferred_day", "") or "").strip()
    p_period = (getattr(x, "preferred_period", "") or "").strip()
    avail = (getattr(x, "availability", "") or "").strip()

    cursor.execute(
        "INSERT INTO subjects (code, name, teacher, section, type, classes_per_week, required_equipment, duration, preferred_day, preferred_period, availability) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clean_code, x.name.strip(), x.teacher.strip(), x.section.strip(), x.type.strip(), x.classes_per_week, req_eq, dur, p_day, p_period, avail)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.put("/api/subjects/{code}", response_model=Subject)
@app.patch("/api/subjects/{code}", response_model=Subject)
def update_subject(code: str, x: Subject, current_user: Dict[str, Any] = Depends(get_current_user)):
    old_clean = unquote(str(code)).strip().lower()
    new_clean = unquote(x.code).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM subjects WHERE LOWER(code) = ?", (old_clean,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"Subject '{code}' not found")

    if new_clean.lower() != old_clean:
        cursor.execute("SELECT code FROM subjects WHERE LOWER(code) = ?", (new_clean.lower(),))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Another subject with this code already exists")

    # Validate referenced teacher and enforce workload capacity hard constraint
    clean_teacher = unquote(x.teacher).strip()
    cursor.execute("SELECT name, workload FROM teachers WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))", (clean_teacher,))
    teacher_row = cursor.fetchone()
    if not teacher_row:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Assigned teacher '{x.teacher}' does not exist")

    canonical_teacher_name = teacher_row["name"]
    teacher_workload_cap = int(teacher_row["workload"])

    # Calculate total classes_per_week across all other subjects assigned to this teacher (excluding this subject)
    cursor.execute(
        "SELECT COALESCE(SUM(classes_per_week), 0) AS total_assigned FROM subjects WHERE LOWER(TRIM(teacher)) = LOWER(TRIM(?)) AND LOWER(TRIM(code)) != LOWER(TRIM(?))",
        (canonical_teacher_name, old_clean)
    )
    current_other_assigned = int(cursor.fetchone()["total_assigned"])
    new_total = current_other_assigned + x.classes_per_week

    if new_total > teacher_workload_cap:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=(
                f"Teacher '{canonical_teacher_name}' workload cap exceeded: "
                f"Assigned total would be {new_total} classes/week (assigned in other subjects: {current_other_assigned}, requested: {x.classes_per_week}), "
                f"which exceeds the maximum capacity of {teacher_workload_cap} classes/week."
            )
        )

    cursor.execute("SELECT name FROM sections WHERE LOWER(name) = ?", (unquote(x.section).strip().lower(),))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"Assigned section '{x.section}' does not exist")

    req_eq = (x.required_equipment or "").strip()
    dur = max(1, getattr(x, "duration", 1) or 1)
    if dur > 1 and x.classes_per_week < dur:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Subject '{x.name}' has duration {dur} periods, so Periods / Week must be at least {dur}."
        )
    p_day = (getattr(x, "preferred_day", "") or "").strip()
    p_period = (getattr(x, "preferred_period", "") or "").strip()
    avail = (getattr(x, "availability", "") or "").strip()

    cursor.execute(
        "UPDATE subjects SET code = ?, name = ?, teacher = ?, section = ?, type = ?, classes_per_week = ?, required_equipment = ?, duration = ?, preferred_day = ?, preferred_period = ?, availability = ? WHERE LOWER(code) = ?",
        (new_clean, x.name.strip(), x.teacher.strip(), x.section.strip(), x.type.strip(), x.classes_per_week, req_eq, dur, p_day, p_period, avail, old_clean)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return x

@app.delete("/api/subjects/{code}")
def delete_subject(code: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(code)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM subjects WHERE LOWER(code) = ?", (clean,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Subject '{code}' not found")
    actual_code = row["code"]
    cursor.execute("DELETE FROM subjects WHERE LOWER(code) = ?", (clean,))
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Subject '{actual_code}' deleted successfully"}

# --- TIME SLOTS MANAGEMENT CRUD ---
@app.get("/api/timeslots", response_model=List[TimeSlot])
def get_timeslots(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT time, type, order_index FROM timeslots ORDER BY order_index ASC, time ASC")
    rows = cursor.fetchall()
    conn.close()
    return [TimeSlot(time=r["time"], type=r["type"] or "Teaching", order_index=r["order_index"] or 1) for r in rows]

@app.post("/api/timeslots", response_model=TimeSlot)
def add_timeslot(x: TimeSlot, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_time = unquote(x.time).strip()
    if not clean_time:
        raise HTTPException(status_code=400, detail="Time slot interval string cannot be empty")
    slot_type = "Break" if x.type.strip().lower() == "break" else "Teaching"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT time FROM timeslots WHERE LOWER(time) = ?", (clean_time.lower(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Time slot interval already exists")
    
    cursor.execute(
        "INSERT INTO timeslots (time, type, order_index) VALUES (?, ?, ?)",
        (clean_time, slot_type, x.order_index)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return TimeSlot(time=clean_time, type=slot_type, order_index=x.order_index)

@app.put("/api/timeslots/{time_val}", response_model=TimeSlot)
@app.patch("/api/timeslots/{time_val}", response_model=TimeSlot)
def update_timeslot(time_val: str, x: TimeSlot, current_user: Dict[str, Any] = Depends(get_current_user)):
    old_clean = unquote(str(time_val)).strip().lower()
    new_clean = unquote(x.time).strip()
    if not new_clean:
        raise HTTPException(status_code=400, detail="Time slot interval string cannot be empty")
    slot_type = "Break" if x.type.strip().lower() == "break" else "Teaching"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT time FROM timeslots WHERE LOWER(time) = ?", (old_clean,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"Time slot '{time_val}' not found")

    if new_clean.lower() != old_clean:
        cursor.execute("SELECT time FROM timeslots WHERE LOWER(time) = ?", (new_clean.lower(),))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Another time slot with this time interval already exists")

    cursor.execute(
        "UPDATE timeslots SET time = ?, type = ?, order_index = ? WHERE LOWER(time) = ?",
        (new_clean, slot_type, x.order_index, old_clean)
    )
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return TimeSlot(time=new_clean, type=slot_type, order_index=x.order_index)

@app.delete("/api/timeslots/{time_val}")
def delete_timeslot(time_val: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean = unquote(str(time_val)).strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT time, type FROM timeslots WHERE LOWER(time) = ?", (clean,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Time slot '{time_val}' not found")
    
    # Check that at least one teaching slot remains
    cursor.execute("SELECT COUNT(*) AS cnt FROM timeslots WHERE LOWER(type) = 'teaching' AND LOWER(time) != ?", (clean,))
    teaching_rem = cursor.fetchone()["cnt"]
    if teaching_rem == 0 and (row["type"] or "").lower() == "teaching":
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot delete this slot: At least one Teaching slot is required in the schedule.")

    actual_time = row["time"]
    cursor.execute("DELETE FROM timeslots WHERE LOWER(time) = ?", (clean,))
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Time slot '{actual_time}' deleted successfully"}

# --- PERSISTENT TIMETABLE STATE ENDPOINTS ---
@app.delete("/api/timetable")
@app.post("/api/timetable/invalidate")
def clear_saved_timetable(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    invalidate_timetable_state(cursor)
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Timetable state invalidated successfully"}

@app.get("/api/timetable", response_model=Result)
def get_saved_timetable(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT json_data FROM timetable_state WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    if row and row["json_data"]:
        try:
            data = json.loads(row["json_data"])
            return Result(**data)
        except Exception:
            pass
    return Result(status="none", message="Data changed — please regenerate the timetable.")

def validate_generated_timetable(
    subjects: List[Subject],
    sections: List[Section],
    section_timetables: Dict[str, List[Day]],
    all_timeslots: List[TimeSlot]
) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
    """
    Modular, reusable verification audit for generated timetables.
    Verifies 4 core criteria:
      1. Period Count Fulfillment: required_periods == generated_periods for all subjects
      2. Allowed Days Compliance: actual scheduled days ⊆ configured available days
      3. Duration / Consecutive Slots: multi-period classes (duration > 1) scheduled consecutively without breaks
      4. Zero Resource Clashes: 0 teacher clashes, 0 room clashes, 0 section clashes college-wide
    Returns:
      (is_all_valid, validation_report, failure_reasons)
    """
    validation_report = []
    failure_reasons = []

    # Map time slots to order indices and teaching slot checks
    time_to_order = {ts.time: (ts.order_index if ts.order_index is not None else idx) for idx, ts in enumerate(all_timeslots)}

    # 1, 2, 3: Per-Section Subject Checks
    for sec in sections:
        sec_tt = section_timetables.get(sec.name, [])
        sec_subjects = [sub for sub in subjects if sub.section.strip().lower() == sec.name.strip().lower()]

        for sub in sec_subjects:
            required_periods = sub.classes_per_week
            dur = max(1, getattr(sub, "duration", 1) or 1)
            sub_avail_text = getattr(sub, "availability", "") or ""
            allowed_days = days_from_text(sub_avail_text) if sub_avail_text.strip() else None

            actual_periods = 0
            actual_days = set()
            day_slot_indices: Dict[str, List[int]] = {}

            for day_obj in sec_tt:
                day_name = day_obj.day
                for s_idx, slot in enumerate(day_obj.slots):
                    matched = False
                    if slot.sessions:
                        for sess in slot.sessions:
                            if sess.subject.strip().lower() == sub.name.strip().lower() and sess.section.strip().lower() == sec.name.strip().lower():
                                matched = True
                                break
                    elif slot.subject.strip().lower() == sub.name.strip().lower() and slot.section.strip().lower() == sec.name.strip().lower():
                        matched = True

                    if matched:
                        actual_periods += 1
                        actual_days.add(day_name)
                        day_slot_indices.setdefault(day_name, []).append(s_idx)

            sub_errors = []

            # 1. Period fulfillment check
            if actual_periods != required_periods:
                sub_errors.append(f"Required {required_periods} periods, generated {actual_periods}")

            # 2. Allowed days compliance check
            if allowed_days is not None:
                disallowed_days = actual_days - allowed_days
                if disallowed_days:
                    sub_errors.append(f"Scheduled on disallowed day(s): {', '.join(sorted(disallowed_days))}")

            # 3. Duration / Consecutive slots check
            if dur > 1 and actual_periods >= dur:
                for day_name, s_indices in day_slot_indices.items():
                    if len(s_indices) % dur != 0:
                        sub_errors.append(f"Scheduled {len(s_indices)} periods on {day_name}, which is not a multiple of duration {dur}")
                    sorted_indices = sorted(s_indices)
                    for k in range(0, len(sorted_indices), dur):
                        block = sorted_indices[k : k + dur]
                        if len(block) == dur:
                            for offset in range(dur):
                                if block[offset] != block[0] + offset:
                                    sub_errors.append(f"Non-consecutive slots on {day_name} (duration {dur})")
                                    break

            is_passed = (len(sub_errors) == 0)
            if not is_passed:
                err_msg = f"Subject '{sub.name}' ({sub.code}) in section '{sec.name}': {'; '.join(sub_errors)}"
                failure_reasons.append(err_msg)

            validation_report.append({
                "section": sec.name,
                "code": sub.code,
                "subject": sub.name,
                "teacher": sub.teacher,
                "type": sub.type,
                "duration": dur,
                "required": required_periods,
                "generated": actual_periods,
                "status": "PASSED" if is_passed else "FAILED",
                "reason": "Satisfied & Verified" if is_passed else "; ".join(sub_errors)
            })

    # 4. Zero Conflict Guarantee across the entire institution
    teacher_occupancy: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    room_occupancy: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    section_occupancy: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    for sec_name, days_list in section_timetables.items():
        for day_obj in days_list:
            for slot in day_obj.slots:
                if slot.slot_type.lower() == "break" or slot.subject.strip().lower() in ("break", "free"):
                    continue
                sess_list = slot.sessions if slot.sessions else [slot]
                for sess in sess_list:
                    if sess.subject.strip().lower() in ("break", "free"):
                        continue
                    key = (day_obj.day, slot.time)
                    t_name = (sess.teacher or "").strip()
                    if t_name and t_name != "-":
                        teacher_occupancy.setdefault(key, []).append((t_name, sec_name, sess.subject))
                    r_name = (sess.room or "").strip()
                    if r_name and r_name != "-":
                        room_occupancy.setdefault(key, []).append((r_name, sec_name, sess.subject))
                    section_occupancy.setdefault(key, []).append((sec_name, sess.subject))

    # Check teacher clashes
    for (day_name, slot_time), entries in teacher_occupancy.items():
        t_counts: Dict[str, List[Tuple[str, str]]] = {}
        for t_name, sec_name, sub_name in entries:
            t_counts.setdefault(t_name.lower(), []).append((sec_name, sub_name))
        for t_lower, occurrences in t_counts.items():
            if len(occurrences) > 1:
                sec_list = [f"{s} ({sub})" for s, sub in occurrences]
                clash_desc = f"Teacher clash: Faculty '{t_lower}' is double-booked on {day_name} at {slot_time} in {', '.join(sec_list)}"
                failure_reasons.append(clash_desc)

    # Check room clashes
    for (day_name, slot_time), entries in room_occupancy.items():
        r_counts: Dict[str, List[Tuple[str, str]]] = {}
        for r_name, sec_name, sub_name in entries:
            r_counts.setdefault(r_name.lower(), []).append((sec_name, sub_name))
        for r_lower, occurrences in r_counts.items():
            if len(occurrences) > 1:
                sec_list = [f"{s} ({sub})" for s, sub in occurrences]
                clash_desc = f"Room clash: Venue '{r_lower}' is double-booked on {day_name} at {slot_time} by {', '.join(sec_list)}"
                failure_reasons.append(clash_desc)

    # Check section clashes
    for (day_name, slot_time), entries in section_occupancy.items():
        s_counts: Dict[str, List[str]] = {}
        for sec_name, sub_name in entries:
            s_counts.setdefault(sec_name.lower(), []).append(sub_name)
        for s_lower, sub_list in s_counts.items():
            if len(sub_list) > 1:
                clash_desc = f"Section clash: Section '{s_lower}' is scheduled for multiple subjects simultaneously on {day_name} at {slot_time}: {', '.join(sub_list)}"
                failure_reasons.append(clash_desc)

    is_all_valid = (len(failure_reasons) == 0) and all(item["status"] == "PASSED" for item in validation_report)
    return is_all_valid, validation_report, failure_reasons


def diagnose_solver_infeasibility(
    classes: list,
    teachers: list,
    rooms: list,
    labs: list,
    sections: list,
    subjects: list,
    teaching_slots: list,
    continuous_pairs: list,
    next_consecutive_slot: dict,
    teacher_by_name: dict,
    section_by_name: dict
) -> Tuple[str, str]:
    """
    Builds a targeted diagnostic CP-SAT model with slack variables on class occurrence constraints
    to pinpoint the exact unschedulable subject, teacher, section, or resource causing infeasibility.
    """
    diag_model = cp_model.CpModel()
    diag_x = {}

    for c, (sub, teacher, section, matching_resources, _) in enumerate(classes):
        t_avail = days_from_text(teacher.availability)
        sub_avail_text = getattr(sub, "availability", "") or ""
        sub_avail = days_from_text(sub_avail_text) if sub_avail_text.strip() else None

        for d, day_name in enumerate(DAYS):
            if day_name not in t_avail:
                continue
            if sub_avail and day_name not in sub_avail:
                continue

            for s_idx in range(len(teaching_slots)):
                for ri, (kind, real_idx, res_obj) in enumerate(matching_resources):
                    r_avail = days_from_text(res_obj.availability)
                    if day_name not in r_avail:
                        continue
                    diag_x[(c, d, s_idx, ri)] = diag_model.NewBoolVar(f"diag_x_c{c}_d{d}_s{s_idx}_r{ri}")

    # Slack variable for each class occurrence: slack[c] == 1 if occurrence c could not be scheduled
    slacks = {}
    for c in range(len(classes)):
        c_vars = [v for (cc, _, _, _), v in diag_x.items() if cc == c]
        sl = diag_model.NewBoolVar(f"diag_slack_{c}")
        slacks[c] = sl
        if c_vars:
            diag_model.Add(sum(c_vars) + sl == 1)
        else:
            diag_model.Add(sl == 1)

    # Hard constraints among scheduled classes
    for d in range(len(DAYS)):
        for s_idx in range(len(teaching_slots)):
            for t in teachers:
                diag_model.AddAtMostOne(
                    v for (c, dd, ss, _), v in diag_x.items()
                    if dd == d and ss == s_idx and classes[c][1].name == t.name
                )
            for sec in sections:
                diag_model.AddAtMostOne(
                    v for (c, dd, ss, _), v in diag_x.items()
                    if dd == d and ss == s_idx and classes[c][2].name == sec.name
                )
            all_phys = [("room", i, r.id) for i, r in enumerate(rooms)] + [("lab", i, l.id) for i, l in enumerate(labs)]
            for kind, real_i, _ in all_phys:
                diag_model.AddAtMostOne(
                    v for (c, dd, ss, ri), v in diag_x.items()
                    if dd == d and ss == s_idx and
                    classes[c][3][ri][0] == kind and classes[c][3][ri][1] == real_i
                )

    for t in teachers:
        t_vars = [v for (c, d, s, ri), v in diag_x.items() if classes[c][1].name == t.name]
        if t_vars:
            diag_model.Add(sum(t_vars) <= t.workload)

    for c_first, c_second in continuous_pairs:
        for d in range(len(DAYS)):
            for s_idx in range(len(teaching_slots)):
                s_next = next_consecutive_slot.get(s_idx)
                for ri in range(len(classes[c_first][3])):
                    v_first = diag_x.get((c_first, d, s_idx, ri))
                    if v_first is None:
                        continue
                    if s_next is None:
                        diag_model.Add(v_first == 0)
                    else:
                        v2 = diag_x.get((c_second, d, s_next, ri))
                        if v2 is None:
                            diag_model.Add(v_first == 0)
                        else:
                            diag_model.Add(v_first == v2)

    # HARD CONSTRAINT 8 in diagnostic model (with resource-aware scaling)
    for sub in subjects:
        dur = max(1, getattr(sub, "duration", 1) or 1)
        eff_classes = max(sub.classes_per_week, dur if dur > 1 else 1)
        num_blocks = math.ceil(eff_classes / dur)
        sub_avail_text = getattr(sub, "availability", "") or ""
        sub_avail = days_from_text(sub_avail_text) if sub_avail_text.strip() else set(DAYS)
        t = teacher_by_name.get(sub.teacher.strip().lower())
        t_avail = days_from_text(t.availability) if t else set(DAYS)
        s = section_by_name.get(sub.section.strip().lower())
        sub_m_res = []
        for c_sub, _, _, m_res, _ in classes:
            if c_sub.code == sub.code:
                sub_m_res = m_res
                break
        res_avail = set()
        for _, _, r_obj in sub_m_res:
            res_avail |= days_from_text(r_obj.availability)
        valid_days = sub_avail & t_avail & res_avail
        if num_blocks <= len(valid_days):
            max_blocks_per_day = 1
        elif len(valid_days) > 0:
            max_blocks_per_day = math.ceil(num_blocks / len(valid_days))
        else:
            max_blocks_per_day = len(teaching_slots)

        for d in range(len(DAYS)):
            sub_vars_day = [v for (c, dd, ss, _), v in diag_x.items() if dd == d and classes[c][0].code == sub.code]
            if sub_vars_day:
                diag_model.Add(sum(sub_vars_day) <= max_blocks_per_day * dur)

    diag_model.Minimize(sum(slacks.values()))

    diag_solver = cp_model.CpSolver()
    diag_solver.parameters.max_time_in_seconds = 8.0
    diag_solver.parameters.num_search_workers = 8
    diag_status = diag_solver.Solve(diag_model)

    unscheduled_by_subject = {}
    if diag_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for c in range(len(classes)):
            if diag_solver.Value(slacks[c]) == 1:
                sub, teacher, section, matching_resources, occ = classes[c]
                sub_code = sub.code
                if sub_code not in unscheduled_by_subject:
                    unscheduled_by_subject[sub_code] = {
                        "subject": sub,
                        "teacher": teacher,
                        "section": section,
                        "matching_resources": matching_resources,
                        "failed_occurrences": []
                    }
                unscheduled_by_subject[sub_code]["failed_occurrences"].append(occ + 1)

    if not unscheduled_by_subject:
        return (
            "No feasible timetable found: Over-constrained schedule.",
            "The combination of faculty workload, availability days, room capacities, and weekly class requirements creates an over-constrained system. Try adding classrooms, relaxing teacher availability days, or reducing classes per week."
        )

    first_code = next(iter(unscheduled_by_subject.keys()))
    first_info = unscheduled_by_subject[first_code]
    first_sub = first_info["subject"]

    msg = f"No feasible timetable found: Scheduling conflict for Subject '{first_sub.code}' ({first_sub.name})."

    conflict_lines = []
    conflict_lines.append(f"Diagnostic Analysis: Identified {len(unscheduled_by_subject)} unschedulable subject(s) due to hard constraint contention:")
    for code, info in unscheduled_by_subject.items():
        s = info["subject"]
        sec = info["section"]
        t = info["teacher"]
        m_res = info["matching_resources"]
        dur = max(1, getattr(s, "duration", 1) or 1)
        stype = getattr(s, "type", "Classroom") or "Classroom"
        failed_cnt = len(info["failed_occurrences"])

        conflict_lines.append(f"\n• Subject [{code}] '{s.name}' (Section: '{sec.name}', Strength: {sec.strength}):")
        conflict_lines.append(f"  - Unschedulable Occurrences: {failed_cnt} of {s.classes_per_week} classes/week (Period indices: {info['failed_occurrences']})")
        conflict_lines.append(f"  - Assigned Teacher: '{t.name}' (Workload Cap: {t.workload} classes/week, Availability: '{t.availability}')")
        conflict_lines.append(f"  - Course Type: {stype}, Block Duration: {dur} period(s), Equipment Requirement: '{getattr(s, 'required_equipment', '') or 'None'}'")
        if m_res:
            res_desc = ", ".join(f"{kind.upper()} {r.id} [cap: {r.capacity}, avail: '{r.availability}']" for kind, _, r in m_res[:4])
            conflict_lines.append(f"  - Qualified Matching Resources ({len(m_res)}): {res_desc}")
            conflict_lines.append(f"  - Root Cause: All suitable facility or teacher periods are fully occupied by competing subjects on the mutually available days.")
        else:
            conflict_lines.append(f"  - Qualified Matching Resources: 0 available. No {stype} meets section strength ({sec.strength}) and equipment requirements.")

    conflict_lines.append("\nRecommended Actions to Resolve:")
    conflict_lines.append("1. Verify teacher availability days and ensure they overlap with room/lab availability.")
    conflict_lines.append("2. Add additional available days or periods to specialized facilities.")
    conflict_lines.append("3. Reduce weekly class requirements or break long blocks into single periods.")

    return msg, "\n".join(conflict_lines)


# ==============================================================================
# OR-Tools CP-SAT Timetable Constraint Solver (Dynamic & Multi-Constraint)
# ==============================================================================
@app.post("/api/generate-timetable", response_model=Result)
def generate_timetable(weights: Weights, current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db()
    cursor = conn.cursor()

    # 1. Fetch live institutional resources from SQLite
    cursor.execute("SELECT name, subject, workload, availability FROM teachers ORDER BY name")
    teachers = [Teacher(**dict(r)) for r in cursor.fetchall()]

    cursor.execute("SELECT id, capacity, location, availability FROM rooms ORDER BY id")
    rooms = [Room(**dict(r)) for r in cursor.fetchall()]

    cursor.execute("SELECT id, capacity, equipment, availability FROM labs ORDER BY id")
    labs = [Lab(**dict(r)) for r in cursor.fetchall()]

    cursor.execute("SELECT name, year, branch, section, strength FROM sections ORDER BY name")
    sections = [Section(**dict(r)) for r in cursor.fetchall()]

    cursor.execute("SELECT code, name, teacher, section, type, classes_per_week, required_equipment, COALESCE(duration, 1) AS duration, COALESCE(preferred_day, '') AS preferred_day, COALESCE(preferred_period, '') AS preferred_period, COALESCE(availability, '') AS availability FROM subjects ORDER BY code")
    subjects = [Subject(**dict(r)) for r in cursor.fetchall()]

    cursor.execute("SELECT time, type, order_index FROM timeslots ORDER BY order_index ASC, time ASC")
    all_timeslots = [TimeSlot(time=r["time"], type=r["type"] or "Teaching", order_index=r["order_index"] or 1) for r in cursor.fetchall()]
    conn.close()

    # --- Pre-Solve Diagnostic Feasibility Validation ---
    if not teachers:
        return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: No teachers configured in database.",
                      conflicts="Please navigate to Data Entry > Teachers and add at least one faculty member.")
    if not rooms and not labs:
        return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: No classrooms or laboratories configured.",
                      conflicts="Please add classrooms or laboratories in the Data Entry tab.")
    if not sections:
        return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: No class sections configured in database.",
                      conflicts="Please add at least one class section (e.g. CSE-3A) in the Data Entry tab.")

    # Filter teaching slots exclusively for class scheduling (Break slots never receive classes)
    teaching_slots = [ts for ts in all_timeslots if ts.type.lower() == "teaching"]
    if not teaching_slots:
        return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: No teaching time slots available.",
                      conflicts="All time slots are designated as Break. Please ensure at least one slot is marked as 'Teaching'.")

    # Map index in teaching_slots to index in all_timeslots for contiguous adjacency checking
    ts_to_all_idx = {}
    for s_idx, ts in enumerate(teaching_slots):
        for all_idx, ats in enumerate(all_timeslots):
            if ats.time == ts.time and ats.order_index == ts.order_index:
                ts_to_all_idx[s_idx] = all_idx
                break

    # next_consecutive_slot[s_idx] is the index of the next teaching slot IF they are consecutive (not separated by Break)
    next_consecutive_slot = {}
    for s_idx in range(len(teaching_slots)):
        if s_idx + 1 < len(teaching_slots):
            all_idx_curr = ts_to_all_idx.get(s_idx)
            all_idx_next = ts_to_all_idx.get(s_idx + 1)
            if all_idx_curr is not None and all_idx_next is not None and all_idx_next == all_idx_curr + 1:
                next_consecutive_slot[s_idx] = s_idx + 1
            else:
                next_consecutive_slot[s_idx] = None
        else:
            next_consecutive_slot[s_idx] = None
    # Calculate maximum unbroken teaching block between breaks
    contiguous_blocks = []
    cur_len = 0
    for ats in all_timeslots:
        if ats.type.lower() == "teaching":
            cur_len += 1
        else:
            if cur_len > 0:
                contiguous_blocks.append(cur_len)
                cur_len = 0
    if cur_len > 0:
        contiguous_blocks.append(cur_len)
    max_unbroken_block = max(contiguous_blocks, default=len(teaching_slots))

    # Pre-solve check 1: Section strength validation
    for sec in sections:
        if sec.strength < 1:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Invalid student strength for section '{sec.name}'.",
                          conflicts=f"Section '{sec.name}' has student strength {sec.strength}. Student strength must be at least 1.")

    # Pre-solve check 2: Subject duration and classes_per_week validation
    for sub in subjects:
        if sub.classes_per_week < 1:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Invalid classes per week for subject '{sub.code}'.",
                          conflicts=f"Subject '{sub.code}' ({sub.name}) has classes_per_week = {sub.classes_per_week}. Classes per week must be at least 1.")
        dur = max(1, getattr(sub, "duration", 1) or 1)
        if dur > max_unbroken_block:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Subject '{sub.code}' duration exceeds maximum continuous block.",
                          conflicts=f"Subject '{sub.code}' ({sub.name}) requires a continuous duration of {dur} period(s), but the maximum unbroken teaching block between breaks in the schedule is only {max_unbroken_block} period(s).")

    # Pre-solve check 3: Teacher availability configuration validation
    for t in teachers:
        t_avail = days_from_text(t.availability)
        if not t_avail:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Invalid availability for teacher '{t.name}'.",
                          conflicts=f"Teacher '{t.name}' has no valid teaching days configured (current value: '{t.availability}'). Allowed days are Monday to Saturday.")

    # If subjects table is empty, auto-generate initial syllabus from teachers and sections
    if not subjects:
        for i, t in enumerate(teachers):
            sec = sections[i % len(sections)]
            subjects.append(
                Subject(
                    code=f"SUB{i+1}",
                    name=t.subject or f"Course {i+1}",
                    teacher=t.name,
                    section=sec.name,
                    type="Classroom",
                    classes_per_week=min(t.workload, len(teaching_slots) * len(DAYS)),
                    required_equipment=""
                )
            )

    teacher_by_name = {t.name.lower(): t for t in teachers}
    section_by_name = {s.name.lower(): s for s in sections}

    # Verify referential integrity and calculate matching resources with equipment check
    def resources_for_subject(sub: Subject, sec: Section) -> List[Tuple[str, int, Any]]:
        stype = (sub.type or "").strip().lower()
        req = getattr(sub, "required_equipment", "") or ""
        if stype == "lab":
            return [("lab", i, l) for i, l in enumerate(labs) if l.capacity >= sec.strength and is_equipment_compatible(req, l.equipment, l.id)]
        elif stype == "library":
            lib_rooms = [("room", i, r) for i, r in enumerate(rooms) if ("lib" in r.id.lower() or "lib" in (r.location or "").lower()) and r.capacity >= sec.strength]
            if lib_rooms:
                return lib_rooms
            return [("room", i, r) for i, r in enumerate(rooms) if r.capacity >= sec.strength]
        else:  # "classroom" or "lecture"
            gen_rooms = [("room", i, r) for i, r in enumerate(rooms) if "lib" not in r.id.lower() and "lib" not in (r.location or "").lower() and r.capacity >= sec.strength]
            if gen_rooms:
                return gen_rooms
            return [("room", i, r) for i, r in enumerate(rooms) if r.capacity >= sec.strength]

    classes = []
    continuous_pairs = []
    for sub in subjects:
        t = teacher_by_name.get(sub.teacher.strip().lower())
        s = section_by_name.get(sub.section.strip().lower())
        if not t:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Invalid teacher assignment for {sub.code}.",
                          conflicts=f"Subject '{sub.code}' ({sub.name}) is assigned to teacher '{sub.teacher}', who does not exist.")
        if not s:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Invalid section assignment for {sub.code}.",
                          conflicts=f"Subject '{sub.code}' ({sub.name}) is assigned to section '{sub.section}', which does not exist.")

        matching_res = resources_for_subject(sub, s)
        stype = (sub.type or "").strip().lower()
        is_lab = (stype == "lab")
        is_lib = (stype == "library")
        target_kind = "Laboratory" if is_lab else ("Library" if is_lib else "Classroom")
        
        # Pre-solve check 3: Room/Lab capacity and equipment compatibility
        if not matching_res:
            if is_lab:
                if not labs:
                    return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: Missing Laboratory resource.",
                                  conflicts=f"Subject '{sub.code}' ({sub.name}) requires a Laboratory, but 0 labs are configured in database.")
                labs_with_cap = [l for l in labs if l.capacity >= s.strength]
                if not labs_with_cap:
                    max_cap = max([l.capacity for l in labs], default=0)
                    return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Capacity mismatch for section '{s.name}'.",
                                  conflicts=f"Section '{s.name}' has student strength {s.strength}, but the maximum available Laboratory capacity is {max_cap}.")
                else:
                    req = getattr(sub, "required_equipment", "") or ""
                    return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Lab equipment mismatch for '{sub.name}'.",
                                  conflicts=f"Subject '{sub.code}' ({sub.name}) requires lab equipment '{req}', which is not matched by any available laboratory with sufficient capacity ({s.strength}). Available labs: {', '.join(f'{l.id} [{l.equipment}]' for l in labs_with_cap)}.")
            else:
                max_cap = max([x.capacity for x in rooms], default=0)
                if not rooms:
                    return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Missing {target_kind} resource.",
                                  conflicts=f"Subject '{sub.code}' ({sub.name}) requires a {target_kind}, but 0 rooms are configured in database.")
                else:
                    return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Capacity mismatch for section '{s.name}'.",
                                  conflicts=f"Section '{s.name}' has student strength {s.strength}, but the maximum available {target_kind} capacity is {max_cap}.")

        # Pre-solve check 4: Mutual availability overlap between teacher, subject, and facilities
        sub_avail_text = getattr(sub, "availability", "") or ""
        sub_avail = days_from_text(sub_avail_text) if sub_avail_text.strip() else set(DAYS)
        t_avail = days_from_text(t.availability) if t else set(DAYS)
        res_avail = set()
        for _, _, r_obj in matching_res:
            res_avail |= days_from_text(r_obj.availability)
        valid_days = sub_avail & t_avail & res_avail
        if not valid_days:
            return Result(
                status="error",
                solver_status="INFEASIBLE",
                message=f"No feasible timetable found: No overlapping days for subject '{sub.code}'.",
                conflicts=f"Subject '{sub.code}' ({sub.name}) has no mutually available days among Teacher '{t.name}' (available: {', '.join(sorted(t_avail)) or 'None'}), Subject availability ({', '.join(sorted(sub_avail))}), and qualified facilities ({', '.join(f'{r.id} [{r.availability}]' for _, _, r in matching_res[:3])})."
            )

        dur = max(1, getattr(sub, "duration", 1) or 1)
        effective_classes = max(sub.classes_per_week, dur if dur > 1 else 1)
        max_sub_slots = len(valid_days) * len(teaching_slots)
        if effective_classes > max_sub_slots:
            return Result(
                status="error",
                solver_status="INFEASIBLE",
                message=f"No feasible timetable found: Subject '{sub.code}' exceeds mutual available days capacity.",
                conflicts=f"Subject '{sub.code}' ({sub.name}) requires {effective_classes} classes/week, but only {max_sub_slots} slots exist across the {len(valid_days)} mutually available days ({', '.join(sorted(valid_days))})."
            )

        sub_c_indices = []
        for occ in range(effective_classes):
            c_idx = len(classes)
            classes.append((sub, t, s, matching_res, occ))
            sub_c_indices.append(c_idx)

        # Pair consecutive occurrences for continuous classes (e.g. 2-hour blocks)
        if dur > 1:
            for b_start in range(0, len(sub_c_indices), dur):
                block = sub_c_indices[b_start : b_start + dur]
                for k in range(len(block) - 1):
                    continuous_pairs.append((block[k], block[k + 1]))

    # Analytical constraint checks before invoking solver
    total_weekly_teaching_slots = len(DAYS) * len(teaching_slots)

    # Pre-solve check 5: Section overload check
    section_workload = {}
    for sub, _, s, _, _ in classes:
        section_workload[s.name] = section_workload.get(s.name, 0) + 1
    for sec_name, count in section_workload.items():
        if count > total_weekly_teaching_slots:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Section '{sec_name}' is overloaded.",
                          conflicts=f"Section '{sec_name}' requires {count} weekly classes, but only {total_weekly_teaching_slots} teaching slots exist per week ({len(DAYS)} days × {len(teaching_slots)} teaching periods).")

    # Pre-solve check 6: Teacher availability & hard workload cap check
    teacher_classes_map = {}
    for sub, t, _, _, _ in classes:
        teacher_classes_map[t.name] = teacher_classes_map.get(t.name, 0) + 1
    for t in teachers:
        t_count = teacher_classes_map.get(t.name, 0)
        assigned_subs = [sub for sub in subjects if sub.teacher.strip().lower() == t.name.lower()]
        assigned_desc = ", ".join(f"{s.code} ({s.classes_per_week} classes)" for s in assigned_subs)
        if t_count > t.workload:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Teacher '{t.name}' workload cap exceeded.",
                          conflicts=f"Teacher '{t.name}' is assigned {t_count} weekly classes across {len(assigned_subs)} subject(s) [{assigned_desc}], which exceeds their maximum workload cap of {t.workload} classes/week.")

        t_avail = days_from_text(t.availability)
        if not t_avail:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Invalid availability for teacher '{t.name}'.",
                          conflicts=f"Teacher '{t.name}' has no valid teaching days configured (current value: '{t.availability}'). Allowed days are Monday to Saturday.")

        max_t_slots = len(t_avail) * len(teaching_slots)
        if t_count > max_t_slots:
            return Result(status="error", solver_status="INFEASIBLE", message=f"No feasible timetable found: Teacher '{t.name}' is overloaded for available days.",
                          conflicts=f"Teacher '{t.name}' is assigned {t_count} weekly classes [{assigned_desc}], but can at most teach {max_t_slots} slots based on availability ({len(t_avail)} available days [{', '.join(sorted(t_avail))}] × {len(teaching_slots)} teaching periods).")

    # Pre-solve check 7: Aggregate room and lab capacity checks
    total_lectures = sum(1 for sub, _, _, _, _ in classes if (sub.type or "").strip().lower() != "lab")
    total_labs_req = sum(1 for sub, _, _, _, _ in classes if (sub.type or "").strip().lower() == "lab")
    total_room_slots = len(rooms) * total_weekly_teaching_slots
    total_lab_slots = len(labs) * total_weekly_teaching_slots

    if total_lectures > total_room_slots:
        return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: Insufficient classrooms.",
                      conflicts=f"Total requested lecture periods ({total_lectures}) exceeds aggregate classroom capacity ({total_room_slots} room-slots). Add more classrooms or teaching slots.")
    if total_labs_req > total_lab_slots:
        return Result(status="error", solver_status="INFEASIBLE", message="No feasible timetable found: Insufficient laboratories.",
                      conflicts=f"Total requested lab periods ({total_labs_req}) exceeds aggregate lab capacity ({total_lab_slots} lab-slots). Add more laboratories or teaching slots.")

    # ==============================================================================
    # Build OR-Tools CP-SAT Constraint Optimization Model
    # ==============================================================================
    model = cp_model.CpModel()

    # Decision variable: x[c, d, s_idx, ri] = 1 if class occurrence c is scheduled on day d, teaching slot s_idx, resource ri
    x = {}
    for c, (sub, teacher, section, matching_resources, _) in enumerate(classes):
        t_avail = days_from_text(teacher.availability)

        # Subject preferred day and day availability checks
        sub_pref_day = (getattr(sub, "preferred_day", "") or "").strip().lower()
        sub_has_pref_day = bool(sub_pref_day and sub_pref_day not in ("any", "all", "none"))

        sub_avail_text = getattr(sub, "availability", "") or ""
        sub_avail = days_from_text(sub_avail_text) if sub_avail_text.strip() else None

        dur = max(1, getattr(sub, "duration", 1) or 1)

        for d, day_name in enumerate(DAYS):
            if day_name not in t_avail:
                continue
            if sub_avail and day_name not in sub_avail:
                continue

            for s_idx, _ in enumerate(teaching_slots):
                for ri, (kind, real_idx, res_obj) in enumerate(matching_resources):
                    r_avail = days_from_text(res_obj.availability)
                    if day_name not in r_avail:
                        continue
                    x[(c, d, s_idx, ri)] = model.NewBoolVar(f"x_c{c}_d{d}_s{s_idx}_r{ri}")

    # HARD CONSTRAINT 1: Every class occurrence must be scheduled exactly once
    for c, (sub, teacher, section, _, _) in enumerate(classes):
        c_vars = [v for (cc, _, _, _), v in x.items() if cc == c]
        if not c_vars:
            return Result(status="error", message=f"No feasible timetable found: Availability conflict for {sub.code}.",
                          conflicts=f"No overlapping available days exist between teacher '{teacher.name}' and qualified rooms for subject '{sub.code}'.")
        model.Add(sum(c_vars) == 1)

    # HARD CONSTRAINT 2: No teacher clashes (at most 1 class per teacher per day and teaching slot)
    for d in range(len(DAYS)):
        for s_idx in range(len(teaching_slots)):
            for t in teachers:
                model.AddAtMostOne(
                    v for (c, dd, ss, _), v in x.items()
                    if dd == d and ss == s_idx and classes[c][1].name == t.name
                )

    # HARD CONSTRAINT 3: No section clashes (at most 1 class per section per day and teaching slot)
    for d in range(len(DAYS)):
        for s_idx in range(len(teaching_slots)):
            for sec in sections:
                model.AddAtMostOne(
                    v for (c, dd, ss, _), v in x.items()
                    if dd == d and ss == s_idx and classes[c][2].name == sec.name
                )

    # HARD CONSTRAINT 4: No room/lab clashes (at most 1 class per physical room/lab per day and teaching slot)
    all_phys = [("room", i, r.id) for i, r in enumerate(rooms)] + [("lab", i, l.id) for i, l in enumerate(labs)]
    for d in range(len(DAYS)):
        for s_idx in range(len(teaching_slots)):
            for kind, real_i, _ in all_phys:
                model.AddAtMostOne(
                    v for (c, dd, ss, ri), v in x.items()
                    if dd == d and ss == s_idx and
                    classes[c][3][ri][0] == kind and classes[c][3][ri][1] == real_i
                )

    # HARD CONSTRAINT 5: Break slots never contain classes
    # (Enforced by construction: decision variables x are strictly bounded to teaching_slots, so Break slots receive zero assignments)

    # HARD CONSTRAINT 6: Strict Teacher Workload Cap
    for t in teachers:
        t_vars = [v for (c, d, s, ri), v in x.items() if classes[c][1].name == t.name]
        if t_vars:
            model.Add(sum(t_vars) <= t.workload)

    # HARD CONSTRAINT 7: Continuous Multi-Hour Classes (same day, consecutive slots without break, same room)
    for c_first, c_second in continuous_pairs:
        for d in range(len(DAYS)):
            for s_idx in range(len(teaching_slots)):
                s_next = next_consecutive_slot.get(s_idx)
                for ri in range(len(classes[c_first][3])):
                    v_first = x.get((c_first, d, s_idx, ri))
                    if v_first is None:
                        continue
                    if s_next is None:
                        # Cannot start a continuous class in a slot without a consecutive next teaching slot
                        model.Add(v_first == 0)
                    else:
                        v_second = x.get((c_second, d, s_next, ri))
                        if v_second is None:
                            model.Add(v_first == 0)
                        else:
                            model.Add(v_first == v_second)

    # HARD CONSTRAINT 8: Distribute blocks across available days
    for sub in subjects:
        dur = max(1, getattr(sub, "duration", 1) or 1)
        eff_classes = max(sub.classes_per_week, dur if dur > 1 else 1)
        num_blocks = math.ceil(eff_classes / dur)
        sub_avail_text = getattr(sub, "availability", "") or ""
        sub_avail = days_from_text(sub_avail_text) if sub_avail_text.strip() else set(DAYS)
        t = teacher_by_name.get(sub.teacher.strip().lower())
        t_avail = days_from_text(t.availability) if t else set(DAYS)
        s = section_by_name.get(sub.section.strip().lower())
        m_res = resources_for_subject(sub, s)
        res_avail = set()
        for _, _, r_obj in m_res:
            res_avail |= days_from_text(r_obj.availability)
        valid_days = sub_avail & t_avail & res_avail

        if num_blocks <= len(valid_days):
            max_blocks_per_day = 1
        elif len(valid_days) > 0:
            max_blocks_per_day = math.ceil(num_blocks / len(valid_days))
        else:
            max_blocks_per_day = len(teaching_slots)

        for d in range(len(DAYS)):
            sub_vars_day = [v for (c, dd, ss, _), v in x.items() if dd == d and classes[c][0].code == sub.code]
            if sub_vars_day:
                model.Add(sum(sub_vars_day) <= max_blocks_per_day * dur)

    # ==============================================================================
    # SOFT CONSTRAINTS & MULTI-OBJECTIVE OPTIMIZATION (Compact Timetable & Min Gaps)
    # ==============================================================================
    objective_terms = []
    num_teaching = len(teaching_slots)

    # 1. Binary Section-Slot Indicator Variables y[sec.name, d, s]
    y = {}
    for sec in sections:
        for d in range(len(DAYS)):
            for s_idx in range(num_teaching):
                sec_slot_vars = [
                    v for (c, dd, ss, _), v in x.items()
                    if dd == d and ss == s_idx and classes[c][2].name == sec.name
                ]
                y_var = model.NewBoolVar(f"y_{sec.name}_d{d}_s{s_idx}")
                if sec_slot_vars:
                    model.Add(y_var == sum(sec_slot_vars))
                else:
                    model.Add(y_var == 0)
                y[(sec.name, d, s_idx)] = y_var

    # 2. ELIMINATE UNNECESSARY FREE PERIODS (Leading Free Periods & Internal Gaps)
    # active_after[s] = 1 if there is any class at slot s or later that day.
    # If active_after[s] == 1 and y[s] == 0, then slot s is an UNNECESSARY FREE PERIOD before or between classes.
    # Post-dismissal periods (after the day's last class) have active_after[s] == 0 and are NOT penalized.
    idle_penalty = max(weights.minimize_gaps, 1) * 100 + 150
    gap_penalty = max(weights.minimize_gaps, 1) * 100 + 150
    for sec in sections:
        for d in range(len(DAYS)):
            y_day = [y[(sec.name, d, s)] for s in range(num_teaching)]
            for s in range(num_teaching):
                act_after = model.NewBoolVar(f"act_after_{sec.name}_d{d}_s{s}")
                model.AddMaxEquality(act_after, y_day[s:])
                objective_terms.append(-idle_penalty * (act_after - y_day[s]))

            for s in range(1, num_teaching - 1):
                has_earlier = model.NewBoolVar(f"early_{sec.name}_d{d}_s{s}")
                has_later = model.NewBoolVar(f"later_{sec.name}_d{d}_s{s}")
                model.AddMaxEquality(has_earlier, y_day[:s])
                model.AddMaxEquality(has_later, y_day[s + 1:])
                gap = model.NewBoolVar(f"gap_{sec.name}_d{d}_s{s}")
                model.Add(gap >= has_earlier + has_later - 1 - y_day[s])
                objective_terms.append(-gap_penalty * gap)

    # 3. FILL AVAILABLE PERIODS COMPACTLY FROM EARLIEST SLOTS (Morning Gradient)
    # Earlier teaching slots get higher reward, strictly packing required classes into slots 0, 1, 2...
    slot_scale = max(weights.morning_preference, 1) * 15 + 25
    for sec in sections:
        for d in range(len(DAYS)):
            for s in range(num_teaching):
                objective_terms.append((num_teaching - s) * slot_scale * y[(sec.name, d, s)])

    # 4. CONSECUTIVE PERIODS BONUS (Clusters classes into contiguous blocks)
    consec_bonus = max(weights.minimize_gaps, 1) * 25 + 30
    for sec in sections:
        for d in range(len(DAYS)):
            for s in range(num_teaching - 1):
                consec = model.NewBoolVar(f"consec_{sec.name}_d{d}_s{s}")
                model.Add(consec <= y[(sec.name, d, s)])
                model.Add(consec <= y[(sec.name, d, s + 1)])
                objective_terms.append(consec_bonus * consec)

    # 5. BALANCED DAILY LOAD (Prevent sparse 1-class days with many free periods)
    for sec in sections:
        sec_classes_cnt = sum(1 for c, (sub, _, s, _, _) in enumerate(classes) if s.name == sec.name)
        min_healthy = min(3, max(1, sec_classes_cnt // len(DAYS)))
        max_healthy = min(num_teaching, math.ceil(sec_classes_cnt / len(DAYS)) + 1)
        for d in range(len(DAYS)):
            y_day = [y[(sec.name, d, s)] for s in range(num_teaching)]
            day_sum = sum(y_day)
            day_active = model.NewBoolVar(f"day_act_{sec.name}_d{d}")
            model.AddMaxEquality(day_active, y_day)

            # Penalize sparse days where section attends for fewer than min_healthy classes
            sparse = model.NewIntVar(0, num_teaching, f"sparse_{sec.name}_d{d}")
            model.Add(sparse >= min_healthy * day_active - day_sum)
            objective_terms.append(-100 * sparse)

            # Soft excess penalty if a single day exceeds max_healthy
            excess = model.NewIntVar(0, num_teaching, f"excess_{sec.name}_d{d}")
            model.Add(excess >= day_sum - max_healthy)
            objective_terms.append(-40 * excess)


    # 6. FACULTY WORKLOAD SPREAD
    if weights.balance_workload > 0:
        for t in teachers:
            for d in range(len(DAYS)):
                t_vars_day = [v for (c, dd, ss, _), v in x.items() if dd == d and classes[c][1].name == t.name]
                if t_vars_day:
                    day_active_t = model.NewBoolVar(f"active_t_{t.name}_d{d}")
                    for v in t_vars_day:
                        model.Add(day_active_t >= v)
                    model.Add(day_active_t <= sum(t_vars_day))
                    objective_terms.append(weights.balance_workload * 10 * day_active_t)

    # 7. SUBJECT DISTRIBUTION ACROSS DAYS (Avoid scheduling duplicate lectures of the same subject on the same day)
    for sub in subjects:
        if sub.type.strip().lower() != "lab" and getattr(sub, "duration", 1) <= 1 and sub.classes_per_week <= len(DAYS):
            for d in range(len(DAYS)):
                sub_vars_day = [v for (c, dd, ss, _), v in x.items() if dd == d and classes[c][0].code == sub.code]
                if len(sub_vars_day) > 1:
                    dup = model.NewBoolVar(f"dup_{sub.code}_d{d}")
                    model.Add(dup >= sum(sub_vars_day) - 1)
                    objective_terms.append(-50 * dup)

    # 8. OPTIONAL PREFERRED PERIOD REWARD (Soft bonus)
    for c, (sub, teacher, section, matching_resources, _) in enumerate(classes):
        sub_pref_period = (getattr(sub, "preferred_period", "") or "").strip().lower()
        if sub_pref_period and sub_pref_period not in ("any", "none", "flexible"):
            for d in range(len(DAYS)):
                for s_idx, ts in enumerate(teaching_slots):
                    if ts.time.strip().lower() == sub_pref_period:
                        for ri in range(len(matching_resources)):
                            v = x.get((c, d, s_idx, ri))
                            if v is not None:
                                objective_terms.append(40 * v)

    # 9. OPTIONAL PREFERRED DAY REWARD FOR MULTI-SESSION SUBJECTS (Soft bonus)
    for c, (sub, teacher, section, matching_resources, _) in enumerate(classes):
        sub_pref_day = (getattr(sub, "preferred_day", "") or "").strip().lower()
        if sub_pref_day and sub_pref_day not in ("any", "all", "none"):
            for d, day_name in enumerate(DAYS):
                if day_name.lower() == sub_pref_day:
                    for s_idx in range(len(teaching_slots)):
                        for ri in range(len(matching_resources)):
                            v = x.get((c, d, s_idx, ri))
                            if v is not None:
                                objective_terms.append(30 * v)

    if objective_terms:
        model.Maximize(sum(objective_terms))

    # Solve CP-SAT model
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 15.0
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        msg, conflicts = diagnose_solver_infeasibility(
            classes=classes,
            teachers=teachers,
            rooms=rooms,
            labs=labs,
            sections=sections,
            subjects=subjects,
            teaching_slots=teaching_slots,
            continuous_pairs=continuous_pairs,
            next_consecutive_slot=next_consecutive_slot,
            teacher_by_name=teacher_by_name,
            section_by_name=section_by_name,
        )
        return Result(
            status="error",
            solver_status="INFEASIBLE",
            message=msg,
            conflicts=conflicts
        )

    # Build schedule mapping: schedule_map[d][s_idx] = list of Session
    schedule_map = {d: {s_idx: [] for s_idx in range(len(teaching_slots))} for d in range(len(DAYS))}
    for (c, d, s_idx, ri), var in x.items():
        if solver.Value(var) == 1:
            sub, teacher, section, matching_resources, _ = classes[c]
            _, _, resource = matching_resources[ri]
            schedule_map[d][s_idx].append(
                Session(
                    subject=sub.name,
                    teacher=teacher.name,
                    room=resource.id,
                    section=section.name,
                    branch=section.branch,
                    year=section.year,
                    section_letter=section.section,
                    duration=getattr(sub, "duration", 1) or 1,
                    session_type=getattr(sub, "type", "Classroom") or "Classroom"
                )
            )

    # 1. Build master / combined multi-slot timetable (All Timetables overview)
    result_timetable = []
    for d, day_name in enumerate(DAYS):
        daily_slots = []
        teaching_idx_counter = 0

        for ts in all_timeslots:
            if ts.type.lower() == "break":
                daily_slots.append(
                    Slot(
                        time=ts.time,
                        subject="Break",
                        teacher="-",
                        room="-",
                        section="",
                        slot_type="Break",
                        sessions=[]
                    )
                )
            else:
                sess_list = schedule_map[d][teaching_idx_counter]
                teaching_idx_counter += 1
                if sess_list:
                    primary = sess_list[0]
                    daily_slots.append(
                        Slot(
                            time=ts.time,
                            subject=primary.subject if len(sess_list) == 1 else ", ".join(item.subject for item in sess_list),
                            teacher=primary.teacher if len(sess_list) == 1 else ", ".join(item.teacher for item in sess_list),
                            room=primary.room if len(sess_list) == 1 else ", ".join(item.room for item in sess_list),
                            section=primary.section if len(sess_list) == 1 else ", ".join(item.section for item in sess_list),
                            slot_type="Teaching",
                            sessions=sess_list
                        )
                    )
                else:
                    daily_slots.append(
                        Slot(
                            time=ts.time,
                            subject="Free",
                            teacher="-",
                            room="-",
                            section="",
                            slot_type="Teaching",
                            sessions=[]
                        )
                    )
        result_timetable.append(Day(day=day_name, slots=daily_slots))

    # 2. Build separate conflict-free timetables for EVERY Section (Branch + Academic Year + Section)
    section_timetables = {}
    for sec in sections:
        sec_days = []
        for d, day_name in enumerate(DAYS):
            sec_slots = []
            teaching_idx_counter = 0

            for ts in all_timeslots:
                if ts.type.lower() == "break":
                    sec_slots.append(
                        Slot(
                            time=ts.time,
                            subject="Break",
                            teacher="-",
                            room="-",
                            section="",
                            slot_type="Break",
                            sessions=[]
                        )
                    )
                else:
                    sess_list = schedule_map[d][teaching_idx_counter]
                    teaching_idx_counter += 1
                    # Isolate sessions belonging exclusively to this specific section
                    sec_sess = [s for s in sess_list if s.section.strip().lower() == sec.name.strip().lower()]
                    if sec_sess:
                        item = sec_sess[0]
                        sec_slots.append(
                            Slot(
                                time=ts.time,
                                subject=item.subject,
                                teacher=item.teacher,
                                room=item.room,
                                section=item.section,
                                slot_type="Teaching",
                                sessions=sec_sess
                            )
                        )
                    else:
                        sec_slots.append(
                            Slot(
                                time=ts.time,
                                subject="Free",
                                teacher="-",
                                room="-",
                                section="",
                                slot_type="Teaching",
                                sessions=[]
                            )
                        )
            sec_days.append(Day(day=day_name, slots=sec_slots))
        section_timetables[sec.name] = sec_days

    total_scheduled = sum(len(schedule_map[d][s_idx]) for d in range(len(DAYS)) for s_idx in range(len(teaching_slots)))
    
    is_optimal = (status == cp_model.OPTIMAL)
    solver_tag = "OPTIMAL" if is_optimal else "FEASIBLE"
    obj_score = int(solver.ObjectiveValue()) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0

    if is_optimal:
        msg = f"Optimal conflict-free timetables generated for all {len(sections)} sections with OR-Tools CP-SAT (Objective Score: {obj_score}). Scheduled {total_scheduled} weekly classes with 0 clashes."
    else:
        msg = f"Feasible conflict-free timetables found within time limit (Objective Score: {obj_score}). All hard constraints satisfied (0 clashes), but further optimization may be possible."

    # ==============================================================================
    # Post-Generation Verification Audit & Period Fulfillment Check
    # ==============================================================================
    is_all_valid, validation_report, failure_reasons = validate_generated_timetable(
        subjects=subjects,
        sections=sections,
        section_timetables=section_timetables,
        all_timeslots=all_timeslots
    )

    if not is_all_valid:
        return Result(
            status="error",
            solver_status="INFEASIBLE",
            message="Timetable validation failed: Required weekly periods or institutional constraints could not be satisfied.",
            conflicts="\n".join(failure_reasons),
            timetable=result_timetable,
            section_timetables=section_timetables,
            sections_info=sections,
            validation_report=validation_report
        )

    res = Result(
        status="success",
        solver_status=solver_tag,
        message=msg,
        timetable=result_timetable,
        section_timetables=section_timetables,
        sections_info=sections,
        validation_report=validation_report
    )

    # Persist complete multi-section timetable state to SQLite
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO timetable_state (id, json_data, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)", (res.model_dump_json(),))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Warning: failed to persist timetable state:", e)

    return res

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
