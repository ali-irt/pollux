import os
import gc
import io
import time
import shutil
import subprocess
import hashlib
import secrets
import re
import logging
import threading
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Query, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
import tempfile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import urllib.request
import json
import ssl
from deep_translator import GoogleTranslator
import jwt
from datetime import datetime, timedelta
from dotenv import load_dotenv
from email_validator import validate_email, EmailNotValidError
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
load_dotenv()
ssl._create_default_https_context = ssl._create_unverified_context
from web_ui.db import get_db, init_db

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename='pollux.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App Configuration
# ---------------------------------------------------------------------------

app = FastAPI(title="VoiceWave", version="2.0.0")

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# ---------------------------------------------------------------------------
# CORS Configuration - HARDENED
# ---------------------------------------------------------------------------

if os.environ.get("ENVIRONMENT") == "development":
    ALLOWED_ORIGINS = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://localhost:8081",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ]
else:
    _env_origins = os.environ.get("ALLOWED_ORIGINS", "")
    ALLOWED_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()]


# ---------------------------------------------------------------------------
# Security Headers Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # ✅ FIXED CSP (Swagger-compatible)
    if os.environ.get("ENVIRONMENT") == "development":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' data: blob:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "font-src 'self' https://cdn.jsdelivr.net;"
        )
    else:
     pass
    return response
# ---------------------------------------------------------------------------
# Request Logging Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all HTTP requests for audit trail."""
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    
    # Log request
    logger.info(
        f"{request.method} {request.url.path} - "
        f"Status: {response.status_code} - "
        f"Duration: {duration:.3f}s - "
        f"IP: {request.client.host if request.client else 'unknown'}"
    )
    
    return response

# ---------------------------------------------------------------------------
# Rate Limit Exception Handler
# ---------------------------------------------------------------------------

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    logger.warning(f"Rate limit exceeded for IP: {request.client.host if request.client else 'unknown'}")
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."},
    )

# ---------------------------------------------------------------------------
# Directory Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
MODELS_DIR = BASE_DIR / "models"
VOICE_SAMPLES_DIR = BASE_DIR / "voice_samples"
DB_PATH = BASE_DIR / "pollux.db"

OUTPUTS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
VOICE_SAMPLES_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWT_SECRET = os.environ.get("POLLUX_JWT_SECRET")
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise ValueError("POLLUX_JWT_SECRET must be set and at least 32 characters")

JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
INITIAL_FREE_CREDITS = 50
CREDITS_PER_GENERATION = 5
PREMIUM_QUALITIES = {"high", "medium"}

# Input length limits
MAX_EMAIL_LENGTH = 254  # RFC 5321
MAX_PASSWORD_LENGTH = 56
MIN_PASSWORD_LENGTH = 8
MAX_TEXT_LENGTH = 5000
MAX_TRANSLATION_LENGTH = 5000
MAX_MUSIC_PROMPT_LENGTH = 1000

init_db()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

security = HTTPBearer()
security_optional = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Password Validation
# ---------------------------------------------------------------------------

def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    Validate password strength against OWASP guidelines.
    
    Requirements:
    - Minimum 8 characters
    - Maximum 128 characters
    - At least one uppercase letter (A-Z)
    - At least one lowercase letter (a-z)
    - At least one number (0-9)
    - At least one special character
    
    Returns:
        tuple: (is_valid, error_message)
    """
    
    # Min length
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    
    # Max length
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"Password must not exceed {MAX_PASSWORD_LENGTH} characters."
    
    # Uppercase
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter (A-Z)."
    
    # Lowercase
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter (a-z)."
    
    # Number
    if not re.search(r'\d', password):
        return False, "Password must contain at least one number (0-9)."
    
    # Special character
    special_chars = r'[!@#$%^&*()_+=\-\[\]{};:\'",.<>?/\\|`~]'
    if not re.search(special_chars, password):
        return False, "Password must contain at least one special character (!@#$%^&*...)."
    
    return True, "Valid"

# ---------------------------------------------------------------------------
# Email Validation
# ---------------------------------------------------------------------------

def validate_and_normalize_email(email: str) -> str:
    """
    Validate and normalize email address.
    
    Args:
        email: Email address to validate
        
    Returns:
        Normalized email address (lowercase)
        
    Raises:
        HTTPException: If email is invalid
    """
    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")
    
    if len(email) > MAX_EMAIL_LENGTH:
        raise HTTPException(status_code=400, detail="Email address too long.")
    
    try:
        valid = validate_email(email)
        return valid.email.lower()
    except EmailNotValidError:
        raise HTTPException(status_code=400, detail="Invalid email format.")

# ---------------------------------------------------------------------------
# File Path Validation
# ---------------------------------------------------------------------------

def validate_filename(filename: str, base_dir: Path) -> Path:
    """
    Validate filename to prevent path traversal attacks.
    
    Args:
        filename: User-provided filename
        base_dir: Base directory for files
        
    Returns:
        Validated Path object
        
    Raises:
        HTTPException: If filename is invalid
    """
    
    # Check for path traversal patterns
    suspicious_patterns = ['..', '~', '\\', '../', '..\\', '/./', '/..']
    for pattern in suspicious_patterns:
        if pattern in filename:
            raise HTTPException(status_code=400, detail="Invalid filename.")
    
    # Ensure filename is string
    if not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    
    # Resolve full path
    file_path = (base_dir / filename).resolve()
    base_resolved = base_dir.resolve()
    
    # Ensure file is within base directory
    try:
        file_path.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    
    return file_path

# ---------------------------------------------------------------------------
# Failed Login Tracking
# ---------------------------------------------------------------------------

def record_failed_login_attempt(email: str, ip_address: str):
    """Record failed login attempt in database."""
    conn = get_db()

    # Insert failed attempt
    conn.execute(
        "INSERT INTO failed_login_attempts (email, attempt_time, ip_address) VALUES (?, ?, ?)",
        (email.lower(), datetime.utcnow().isoformat(), ip_address)
    )

    # Count attempts in last 15 minutes
    cutoff = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM failed_login_attempts WHERE email = ? AND attempt_time > ?",
        (email.lower(), cutoff)
    ).fetchone()['cnt']

    if count >= 5:
        logger.warning(f"High failed login count ({count}) for {email} — not locking (open access mode)")

    conn.commit()
    conn.close()


def is_account_locked(email: str) -> tuple[bool, str]:
    """Check if account is locked."""
    conn = get_db()
    result = conn.execute(
        "SELECT locked_until FROM locked_accounts WHERE email = ? AND locked_until > ?",
        (email.lower(), datetime.utcnow().isoformat())
    ).fetchone()
    conn.close()

    if result:
        return True, "Account locked due to too many failed attempts. Try again after 15 minutes."
    return False, ""


def clear_login_attempts(email: str):
    """Clear failed attempts after successful login."""
    conn = get_db()
    conn.execute("DELETE FROM failed_login_attempts WHERE email = ?", (email.lower(),))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Password Hashing and Verification
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}:{hashed}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":")
        return hashlib.sha256((password + salt).encode()).hexdigest() == hashed
    except Exception:
        return False

# ---------------------------------------------------------------------------
# JWT Token Management
# ---------------------------------------------------------------------------

def create_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.utcnow() + timedelta(hours=24),
        "iat": datetime.utcnow(),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=90),
        "iat": datetime.utcnow(),
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": True},
        )
        
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type.")
        
        user_id = int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")

    # Fetch user from DB
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="User not found.")

    return dict(user)  # return a dict, not None


def get_current_user_audio(
    token: str = Query(default=None),
    credentials: HTTPAuthorizationCredentials = Depends(security_optional),
):
    """Auth for audio endpoints: accepts Bearer header OR ?token= query param."""
    raw = None
    if credentials:
        raw = credentials.credentials
    elif token:
        raw = token
    else:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    try:
        payload = jwt.decode(raw, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                             options={"verify_exp": True})
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type.")
        user_id = int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    return dict(user)


def user_remaining(user: dict) -> int:
    """Returns generations remaining. -1 = unlimited (paid plan)."""
    if user["is_premium"]:
        return -1
    return user["credits"]
from fastapi.responses import JSONResponse

def freemium_error(user: dict):
    """Raise a structured 403 when the free limit is hit."""
    return JSONResponse(
        status_code=403,
        content={
            "error_code": "INSUFFICIENT_CREDITS",
            "message": f"You have {user['credits']} credits. Each generation costs {CREDITS_PER_GENERATION} credits. Please top up or upgrade to continue.",
            "credits_remaining": user["credits"],
            "upgrade_url": "/pricing",
        },
    )


def premium_voice_error():
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "PREMIUM_VOICE_LOCKED",
            "message": "This voice requires a premium plan.",
            "upgrade_url": "/pricing",
        },
    )


def premium_feature_error(feature: str = "This feature"):
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "PREMIUM_REQUIRED",
            "message": f"{feature} requires a premium plan.",
            "upgrade_url": "/pricing",
        },
    )

# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------


def is_premium_voice(model_info: dict) -> bool:
    return model_info.get("quality", "") in PREMIUM_QUALITIES


def load_voices_json() -> dict:
    voices_json_path = MODELS_DIR / "voices.json"
    if not voices_json_path.exists():
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/voices.json"
        urllib.request.urlretrieve(url, voices_json_path)
    with open(voices_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def annotate_voices(voices: dict, is_premium_user: bool) -> dict:
    for info in voices.values():
        premium = is_premium_voice(info)
        info["is_premium"] = premium
        info["locked"] = premium and not is_premium_user
    return voices


# ---------------------------------------------------------------------------
# Windows-compatible binary resolution
# ---------------------------------------------------------------------------


def resolve_bin(name: str) -> str:
    for subdir in ("Scripts", "bin"):
        for suffix in (".exe", ""):
            candidate = BASE_DIR / ".venv" / subdir / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
    return name


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/register", summary="Register a new account")
def register(req: RegisterRequest):
    # Validate email
    try:
        req.email = validate_and_normalize_email(req.email)
    except HTTPException:
        raise
    
    # Validate password
    if not req.password:
        raise HTTPException(status_code=400, detail="Password is required.")
    
    is_valid, error_msg = validate_password_strength(req.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    
    # Check for duplicate email
    conn = get_db()
    if conn.execute(
        "SELECT id FROM users WHERE email = ?", (req.email,)
    ).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Email already registered.")
    
    # Create user
    now = datetime.utcnow().isoformat()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, plan, generation_count, credits, is_premium, created_at) VALUES (?, ?, 'free', 0, ?, ?, ?)",
        (
            req.email,
            hash_password(req.password),
            INITIAL_FREE_CREDITS,
            False,
            now,
        ),
    )
    user_id = cursor.lastrowid
    assert user_id is not None
    conn.commit()
    conn.close()

    # Generate tokens
    access_token = create_token(user_id, req.email)
    refresh_token = create_refresh_token(user_id, req.email)

    logger.info(f"User registered: {req.email}")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "email": req.email,
            "plan": "free",
            "generation_count": 0,
            "credits": INITIAL_FREE_CREDITS,
            "is_premium": False,
            "generations_remaining": INITIAL_FREE_CREDITS,
        },
    }


@app.post("/api/auth/login", summary="Login and receive JWT tokens")
@limiter.limit("10/minute")
def login(req: LoginRequest, request: Request):
    # Warn on repeated failures but never block login
    is_locked, msg = is_account_locked(req.email)
    if is_locked:
        logger.warning(f"Login allowed despite lockout flag for {req.email} (open access mode)")
    
    # Validate credentials
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?", (req.email.lower(),)
    ).fetchone()
    conn.close()
    
    if not user or not verify_password(req.password, user["password_hash"]):
        # Record failed attempt
        ip_address = request.client.host if request.client else "unknown"
        record_failed_login_attempt(req.email, ip_address)
        logger.warning(f"Failed login attempt for: {req.email}")
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    
    # Clear failed attempts on successful login
    clear_login_attempts(req.email)
    
    # Generate tokens
    u = dict(user)
    access_token = create_token(u["id"], u["email"])
    refresh_token = create_refresh_token(u["id"], u["email"])

    logger.info(f"User logged in: {req.email}")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "email": u["email"],
            "plan": u["plan"],
            "generation_count": u["generation_count"],
            "credits": u["credits"],
            "is_premium": u["is_premium"],
            "generations_remaining": user_remaining(u),
        },
    }


class RefreshTokenRequest(BaseModel):
    refresh_token: str


@app.post("/api/auth/refresh", summary="Refresh access token")
def refresh_access_token(req: RefreshTokenRequest):
    try:
        payload = jwt.decode(
            req.refresh_token, 
            JWT_SECRET, 
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": True}
        )

        # Verify this is a refresh token
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type.")

        user_id = int(payload["sub"])
        email = payload["email"]

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    # Verify user still exists
    conn = get_db()
    user = conn.execute(
        "SELECT id, email FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="User not found.")

    # Generate new access token
    new_access_token = create_token(user["id"], user["email"])

    return {"access_token": new_access_token, "token_type": "bearer"}


@app.get("/api/auth/me", summary="Get current user info")
def get_me(user=Depends(get_current_user)):
    return {
        "id": user["id"],
        "email": user["email"],
        "plan": user["plan"],
        "generation_count": user["generation_count"],
        "credits": user["credits"],
        "is_premium": user["is_premium"],
        "generations_remaining": user_remaining(user),
        "member_since": user["created_at"],
    }

@app.post("/api/auth/change-password", summary="Change account password")
def change_password(req: ChangePasswordRequest, user=Depends(get_current_user)):
    # Trim spaces
    current_password = req.current_password.strip()
    new_password = req.new_password.strip()
    
    # Verify current password
    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    
    # Ensure new password is different
    if verify_password(new_password, user["password_hash"]):
        raise HTTPException(
            status_code=400,
            detail="New password cannot be the same as the current password."
        )
    
    # Validate password strength
    is_valid, error_msg = validate_password_strength(new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    
    # Hash and save
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user["id"]),
    )
    conn.commit()
    conn.close()
    
    logger.info(f"Password changed for user: {user['email']}")
    
    return {"success": True, "message": "Password updated successfully."}
# ---------------------------------------------------------------------------
# User stats dashboard
# ---------------------------------------------------------------------------


@app.get("/api/stats", summary="Get usage stats for the current user")
def get_stats(user=Depends(get_current_user)):
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM generations WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    last = conn.execute(
        "SELECT created_at FROM generations WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    models_used = conn.execute(
        "SELECT model, COUNT(*) as cnt FROM generations WHERE user_id = ? GROUP BY model ORDER BY cnt DESC LIMIT 5",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {
        "plan": user["plan"],
        "generation_count": user["generation_count"],
        "generations_remaining": user_remaining(user),
        "credits": user["credits"],
        "is_premium": user["is_premium"],
        "total_in_history": total,
        "last_generation": last["created_at"] if last else None,
        "top_voices": [{"model": r["model"], "count": r["cnt"]} for r in models_used],
        "member_since": user["created_at"],
    }


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------
@app.get("/api/voices", summary="All voices with premium flags")
def get_voices(user=Depends(get_current_user)):
    voices = annotate_voices(load_voices_json(), user["is_premium"])
    return list(voices.values())  # convert dict to array


@app.get("/api/voices/free", summary="Free-tier voices only")
def get_free_voices(user=Depends(get_current_user)):
    voices = annotate_voices(load_voices_json(), user["is_premium"])
    filtered = [v for v in voices.values() if not v.get("is_premium", False)]
    return filtered  # always return list, empty if no free voices


@app.get("/api/voices/premium", summary="Premium voices only")
def get_premium_voices(user=Depends(get_current_user)):
    voices = annotate_voices(load_voices_json(), user["is_premium"])
    filtered = [v for v in voices.values() if v.get("is_premium", False)]
    return filtered  # always return list, empty if no premium voices

# ---------------------------------------------------------------------------
# TTS generation
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    text: str
    model: str
    speed: float = 1.0    # 0.5x to 2.0x playback rate
    pitch_hz: int = 0     # -10 to +10 Hz (edge-tts only)


# ---------------------------------------------------------------------------
# Synthesis helper (shared by single + batch generation)
# ---------------------------------------------------------------------------

def _synthesize_audio(
    model_info: dict,
    model_id: str,
    text: str,
    out_path: Path,
    speed: float = 1.0,
    pitch_hz: int = 0,
):
    """Synthesize a single audio clip using edge-tts or piper."""
    is_edge = model_info.get("engine") == "edge-tts"

    if is_edge:
        rate_pct = round((speed - 1.0) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        pitch_str = f"+{pitch_hz}Hz" if pitch_hz >= 0 else f"{pitch_hz}Hz"
        cmd = [
            resolve_bin("edge-tts"),
            "--voice", model_info.get("edge_voice", "ur-PK-UzmaNeural"),
            "--pitch", pitch_str,
            "--rate", rate_str,
            "--text", text,
            "--write-media", str(out_path),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"edge-tts failed: {stderr}")
    else:
        # Download model files on demand
        for file_path, file_meta in model_info.get("files", {}).items():
            dest = MODELS_DIR / file_path.split("/")[-1]
            if not dest.exists():
                dl_url = (
                    file_meta.get("url")
                    or f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{file_path}"
                )
                urllib.request.urlretrieve(dl_url, dest)

        model_onnx = MODELS_DIR / f"{model_id}.onnx"
        length_scale = round(1.0 / max(speed, 0.1), 3)  # piper: higher = slower

        try:
            from piper import PiperVoice
            import wave
            voice = PiperVoice.load(str(model_onnx))
            with wave.open(str(out_path), "wb") as wav_file:
                voice.synthesize_wav(text, wav_file)
        except ImportError:
            cmd = [
                resolve_bin("piper"),
                "--model", str(model_onnx),
                "--output_file", str(out_path),
                "--length-scale", str(length_scale),
            ]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            _, stderr = proc.communicate(input=text)
            if proc.returncode != 0:
                raise HTTPException(status_code=500, detail=f"piper CLI failed: {stderr}")


@app.post("/api/generate", summary="Generate audio from text")
@limiter.limit("20/minute")
def generate_audio(req: GenerateRequest, request: Request, user=Depends(get_current_user)):
    # INPUT VALIDATION (before expensive operations)
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    if len(req.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds maximum length of {MAX_TEXT_LENGTH} characters."
        )

    # Load voices
    voices_info = load_voices_json()
    if req.model not in voices_info:
        raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")

    model_info = voices_info[req.model]

    # All features free — no credit or premium gates

    # Generate to temp file, stream back, discard
    is_edge = model_info.get("engine") == "edge-tts"
    ext = ".mp3" if is_edge else ".wav"
    media_type = "audio/mpeg" if is_edge else "audio/wav"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        _synthesize_audio(
            model_info, req.model, req.text, tmp_path,
            speed=max(0.5, min(2.0, req.speed)),
            pitch_hz=max(-10, min(10, req.pitch_hz)),
        )
        audio_bytes = tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    # Record history (no file stored)
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], "", req.model, req.text[:100], now),
    )
    conn.commit()
    conn.close()

    logger.info(f"Audio generated for user {user['id']}: {req.model}")
    return Response(content=audio_bytes, media_type=media_type,
                    headers={"Content-Disposition": f"inline; filename=output{ext}"})


# ---------------------------------------------------------------------------
# History (paginated)
# ---------------------------------------------------------------------------


@app.get("/api/history", summary="Paginated generation history")
def get_history(
    user=Depends(get_current_user),
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    per_page: int = Query(default=20, ge=1, le=100, description="Items per page"),
):
    offset = (page - 1) * per_page
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM generations WHERE user_id = ? AND model NOT IN ('__batch_zip__')",
        (user["id"],),
    ).fetchone()["cnt"]
    rows = conn.execute(
        "SELECT * FROM generations WHERE user_id = ? AND model NOT IN ('__batch_zip__') ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user["id"], per_page, offset),
    ).fetchall()
    conn.close()
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, -(-total // per_page)),
        "items": [dict(r) for r in rows],
    }


@app.delete("/api/history/{entry_id}", summary="Delete a history entry")
def delete_history_entry(entry_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM generations WHERE id = ? AND user_id = ?", (entry_id, user["id"])
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="History entry not found.")
    conn.execute("DELETE FROM generations WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    logger.info(f"History entry {entry_id} deleted for user {user['email']}")
    return {"success": True, "deleted_id": entry_id}


@app.delete("/api/history", summary="Clear all history for current user")
def clear_history(user=Depends(get_current_user)):
    conn = get_db()
    conn.execute("DELETE FROM generations WHERE user_id = ?", (user["id"],))
    conn.commit()
    conn.close()
    
    logger.info(f"All history cleared for user {user['email']}: {len(rows)} items")
    
    return {"success": True, "deleted_count": len(rows)}


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


SUPPORTED_LANGUAGES = set(GoogleTranslator(source="auto", target="en").get_supported_languages(as_dict=True).values())  # type: ignore[attr-defined]


class TranslateRequest(BaseModel):
    text: str
    target_lang: str

@app.post("/api/translate", summary="Translate text to target language")
def translate_text(request: TranslateRequest, user=Depends(get_current_user)):

    # Validate empty text
    if not request.text or not request.text.strip():
        raise HTTPException(
            status_code=400,
            detail="Text cannot be empty."
        )

    # Validate length
    if len(request.text) > MAX_TRANSLATION_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds maximum length of {MAX_TRANSLATION_LENGTH} characters."
        )

    # Validate target language
    target_lang = request.target_lang.strip().lower()
    if not target_lang:
        raise HTTPException(
            status_code=400,
            detail="Target language is required."
        )
    if target_lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language code: {target_lang}"
        )

    # Perform translation
    try:
        translated = GoogleTranslator(source="auto", target=target_lang).translate(request.text.strip())
        return {"success": True, "translated_text": translated}
    except Exception as e:
        logger.error(f"Translation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Translation service error")
# ---------------------------------------------------------------------------
# Music generation (premium only)
# ---------------------------------------------------------------------------

_music_cache_ready = False
_music_load_lock = threading.Lock()


def _prefetch_music_model():
    global _music_cache_ready
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id="facebook/musicgen-small")
        _music_cache_ready = True
        logger.info("MusicGen model files cached to disk")
    except Exception as e:
        logger.warning(f"MusicGen pre-cache failed (will download on first use): {e}")


def _prefetch_bark_model():
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id="suno/bark-small")
        logger.info("Bark model files cached to disk")
    except Exception as e:
        logger.warning(f"Bark pre-cache failed (will download on first use): {e}")


@app.on_event("startup")
def startup_event():
    threading.Thread(target=_prefetch_music_model, daemon=True).start()
    threading.Thread(target=_prefetch_bark_model, daemon=True).start()


class MusicGenerateRequest(BaseModel):
    prompt: str
    duration: int = 10


@app.post("/api/generate_music", summary="Generate music from a text prompt (premium)")
def generate_music(request: MusicGenerateRequest, user=Depends(get_current_user)):
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    if len(request.prompt) > MAX_MUSIC_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt exceeds maximum length of {MAX_MUSIC_PROMPT_LENGTH} characters."
        )

    with _music_load_lock:
        try:
            import torch
            import scipy.io.wavfile
            from transformers import AutoProcessor, MusicgenForConditionalGeneration

            if torch.backends.mps.is_available():
                device = "mps"
                dtype = torch.float32
            elif torch.cuda.is_available():
                device = "cuda"
                dtype = torch.float16
            else:
                device = "cpu"
                dtype = torch.float32

            processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
            model = MusicgenForConditionalGeneration.from_pretrained(
                "facebook/musicgen-small",
                torch_dtype=dtype,
                low_cpu_mem_usage=True,
            ).to(device)

            try:
                inputs = processor(
                    text=[request.prompt], padding=True, return_tensors="pt"
                ).to(device)
                audio_values = model.generate(
                    **inputs, max_new_tokens=int(request.duration * 25.6)
                )
                sampling_rate = model.config.audio_encoder.sampling_rate
                audio_np = audio_values[0, 0].cpu().numpy()
            finally:
                del model
                del inputs
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

            buf = io.BytesIO()
            scipy.io.wavfile.write(buf, rate=sampling_rate, data=audio_np)
            audio_bytes = buf.getvalue()

            conn = get_db()
            conn.execute(
                "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
            )
            conn.execute(
                "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
                (user["id"], "", "musicgen-small", request.prompt[:100], datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()

            logger.info(f"Music generated for user {user['email']}")
            return Response(content=audio_bytes, media_type="audio/wav",
                            headers={"Content-Disposition": "inline; filename=music.wav"})
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"MusicGen dependencies missing: {e}")
        except Exception as e:
            logger.error(f"Music generation error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Music generation error: {str(e)}")


# ---------------------------------------------------------------------------
# Local Music Enhancement Studio (no external APIs — uses librosa + scipy)
# ---------------------------------------------------------------------------


ENHANCE_ALLOWED_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
ENHANCE_MAX_SIZE = 50 * 1024 * 1024  # 50 MB


@app.post("/api/enhance_audio", summary="Apply local audio effects (normalize, fade, reverb, pitch, speed)")
async def enhance_audio(
    file: UploadFile = File(..., description="Audio file to enhance"),
    normalize: bool = Form(default=True),
    fade_in: float = Form(default=0.0),
    fade_out: float = Form(default=0.0),
    reverb_amount: float = Form(default=0.0),
    pitch_steps: int = Form(default=0),
    speed_factor: float = Form(default=1.0),
    user=Depends(get_current_user),
):
    try:
        import librosa
        import librosa.effects
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Audio processing libraries missing: {e}")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    ext = Path(file.filename).suffix.lower()
    if ext not in ENHANCE_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{ext}'. Allowed: {ENHANCE_ALLOWED_EXT}")

    content = await file.read()
    if len(content) > ENHANCE_MAX_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit.")

    # Clamp params
    fade_in = max(0.0, min(5.0, fade_in))
    fade_out = max(0.0, min(5.0, fade_out))
    reverb_amount = max(0.0, min(1.0, reverb_amount))
    pitch_steps = max(-6, min(6, pitch_steps))
    speed_factor = max(0.5, min(2.0, speed_factor))

    # Load from bytes via temp file
    tmp_in_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_in_path = tmp.name
        try:
            y, sr = librosa.load(tmp_in_path, sr=None, mono=False)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load audio: {e}")
    finally:
        if tmp_in_path:
            try:
                Path(tmp_in_path).unlink()
            except Exception:
                pass

    # Ensure shape is (channels, samples) for multi-channel support
    if y.ndim == 1:
        y = y[np.newaxis, :]

    try:
        # Process each channel independently
        processed_channels = []
        for ch in range(y.shape[0]):
            audio = y[ch].copy()

            # 1. Time-stretch (pitch-preserving speed change)
            if abs(speed_factor - 1.0) > 0.01:
                audio = librosa.effects.time_stretch(audio, rate=speed_factor)

            # 2. Pitch shift
            if pitch_steps != 0:
                audio = librosa.effects.pitch_shift(audio, sr=sr, n_steps=pitch_steps)

            # 3. Fade in
            if fade_in > 0.0:
                fade_samples = int(fade_in * sr)
                fade_samples = min(fade_samples, len(audio))
                ramp = np.linspace(0.0, 1.0, fade_samples)
                audio[:fade_samples] *= ramp

            # 4. Fade out
            if fade_out > 0.0:
                fade_samples = int(fade_out * sr)
                fade_samples = min(fade_samples, len(audio))
                ramp = np.linspace(1.0, 0.0, fade_samples)
                audio[-fade_samples:] *= ramp

            # 5. Simple algorithmic reverb (comb-filter delay network)
            if reverb_amount > 0.0:
                delays_ms = [29, 37, 43, 53]  # prime delay lengths (ms)
                wet = np.zeros_like(audio)
                for d_ms in delays_ms:
                    delay_samples = int(d_ms * sr / 1000)
                    if delay_samples < len(audio):
                        decay = reverb_amount * 0.5
                        padded = np.zeros(len(audio))
                        padded[delay_samples:] = audio[: len(audio) - delay_samples] * decay
                        wet += padded
                audio = audio + wet * reverb_amount

            processed_channels.append(audio)

        # Stack channels back
        result = np.stack(processed_channels, axis=0)

        # 6. Normalize
        if normalize:
            peak = np.max(np.abs(result))
            if peak > 0:
                result = result / peak * 0.95

        # Squeeze mono back to 1D
        if result.shape[0] == 1:
            result = result[0]
        else:
            result = result.T  # (N, channels) for soundfile

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Enhancement processing failed: {e}")

    # Write to BytesIO and stream back
    buf = io.BytesIO()
    try:
        sf.write(buf, result, sr, format="WAV")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to encode enhanced audio: {e}")

    conn = get_db()
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], "", "__enhanced__", f"Enhanced: {file.filename}", datetime.utcnow().isoformat()),
    )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    conn.commit()
    conn.close()

    logger.info(f"Audio enhanced for user {user['email']}")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"Content-Disposition": "inline; filename=enhanced.wav"})


# ---------------------------------------------------------------------------
# Speech-to-Text  (local Whisper via HuggingFace transformers)
# ---------------------------------------------------------------------------

_whisper_pipe = None
_whisper_model_id: str | None = None
MAX_TRANSCRIBE_SIZE = 50 * 1024 * 1024  # 50 MB

WHISPER_ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".flac", ".webm", ".opus"}
WHISPER_ALLOWED_SIZES = {"tiny", "base", "small"}


def _load_whisper(model_size: str):
    global _whisper_pipe, _whisper_model_id
    model_id = f"openai/whisper-{model_size}"
    if _whisper_pipe is not None and _whisper_model_id == model_id:
        return _whisper_pipe

    try:
        import torch
        from transformers import pipeline as hf_pipeline

        device = "cpu"
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"

        _whisper_pipe = hf_pipeline(
            "automatic-speech-recognition",
            model=model_id,
            device=device,
            return_timestamps=True,
        )
        _whisper_model_id = model_id
        logger.info(f"Whisper {model_id} loaded on {device}")
        return _whisper_pipe

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Whisper dependencies missing: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load Whisper model: {e}")


@app.post("/api/transcribe", summary="Transcribe audio to text using local Whisper (no external API)")
async def transcribe_audio(
    file: UploadFile = File(..., description="Audio file to transcribe"),
    model_size: str = Form(default="base", description="Whisper model: tiny | base | small"),
    language: str = Form(default="auto", description="Language code (e.g. 'en') or 'auto' for detection"),
    user=Depends(get_current_user),
):
    # Validate model size
    if model_size not in WHISPER_ALLOWED_SIZES:
        raise HTTPException(status_code=400, detail=f"model_size must be one of: {WHISPER_ALLOWED_SIZES}")

    # Validate file extension
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    ext = Path(file.filename).suffix.lower()
    if ext not in WHISPER_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format '{ext}'. Allowed: {WHISPER_ALLOWED_EXT}",
        )

    # Read and size-check
    content = await file.read()
    if len(content) > MAX_TRANSCRIBE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit.")

    # Write to temp file (Whisper needs a file path)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        pipe = _load_whisper(model_size)

        generate_kwargs: dict = {"task": "transcribe"}
        if language != "auto":
            generate_kwargs["language"] = language

        result = pipe(tmp_path, generate_kwargs=generate_kwargs, return_timestamps=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Transcription error for user {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass

    full_text: str = (result.get("text") or "").strip()
    chunks: list = result.get("chunks") or []

    segments = []
    for chunk in chunks:
        ts = chunk.get("timestamp") or (0, 0)
        segments.append({
            "start": ts[0] if ts[0] is not None else 0,
            "end": ts[1] if ts[1] is not None else 0,
            "text": (chunk.get("text") or "").strip(),
        })

    logger.info(f"Transcription done for user {user['email']}: {len(full_text)} chars, {len(segments)} segments")

    return {
        "success": True,
        "text": full_text,
        "segments": segments,
        "model": f"openai/whisper-{model_size}",
        "language_hint": language,
        "word_count": len(full_text.split()) if full_text else 0,
        "character_count": len(full_text),
        "segment_count": len(segments),
    }




# ---------------------------------------------------------------------------
# Song Generation  (Suno Bark — vocals + music, fully local, no external API)
# ---------------------------------------------------------------------------

_bark_processor = None
_bark_model = None
_bark_device = "cpu"

BARK_VOICE_PRESETS = {
    "en_singer_1": "v2/en_speaker_1",
    "en_singer_2": "v2/en_speaker_3",
    "en_singer_3": "v2/en_speaker_6",
    "en_singer_4": "v2/en_speaker_9",
    "en_female_1": "v2/en_speaker_0",
    "en_female_2": "v2/en_speaker_8",
    "zh_singer_1": "v2/zh_speaker_2",
    "es_singer_1": "v2/es_speaker_2",
    "fr_singer_1": "v2/fr_speaker_2",
    "de_singer_1": "v2/de_speaker_2",
    "hi_singer_1": "v2/hi_speaker_2",
    "ar_singer_1": "v2/en_speaker_6",   # Bark doesn't have Arabic presets; fallback
    "tr_singer_1": "v2/tr_speaker_2",
    "ru_singer_1": "v2/ru_speaker_2",
    "pt_singer_1": "v2/pt_speaker_2",
}

SONG_STYLE_PROMPTS = {
    "pop":        "[upbeat pop music]",
    "ballad":     "[slow piano ballad]",
    "hiphop":     "[hip hop beat]",
    "rock":       "[electric guitar rock]",
    "jazz":       "[jazz background]",
    "rnb":        "[smooth R&B rhythm]",
    "electronic": "[electronic synth beat]",
    "acoustic":   "[acoustic guitar]",
    "classical":  "[orchestral classical music]",
    "none":       "",
}

MAX_SONG_LYRICS_LENGTH = 800


class SongGenerateRequest(BaseModel):
    lyrics: str
    voice_preset: str = "en_singer_3"
    style: str = "pop"             # key from SONG_STYLE_PROMPTS
    quality: str = "small"         # "small" (faster) or "large" (better)


def _load_bark(quality: str = "small"):
    global _bark_processor, _bark_model, _bark_device

    model_id = "suno/bark-small" if quality == "small" else "suno/bark"

    if _bark_processor is not None and getattr(_bark_model, "_model_id", None) == model_id:
        return _bark_processor, _bark_model

    try:
        import torch
        from transformers import AutoProcessor, BarkModel as _BarkModel

        device = "cpu"
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"

        _bark_device = device
        _bark_processor = AutoProcessor.from_pretrained(model_id)
        _bark_model = _BarkModel.from_pretrained(model_id).to(device)
        _bark_model._model_id = model_id          # tag for cache check
        logger.info(f"Bark {model_id} loaded on {device}")
        return _bark_processor, _bark_model

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Bark dependencies missing: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load Bark model: {e}")


def _format_song_script(lyrics: str, style: str) -> str:
    """
    Wrap lyrics with ♪ singing markers and prepend style prompt.
    Handles multi-line input — each non-empty line becomes a sung phrase.
    """
    style_tag = SONG_STYLE_PROMPTS.get(style, "")
    lines = [l.strip() for l in lyrics.strip().splitlines() if l.strip()]

    # If the user already used ♪ marks, respect them
    if "♪" in lyrics:
        body = "\n".join(lines)
    else:
        body = " ♪\n♪ ".join(lines)
        body = f"♪ {body} ♪"

    return f"{style_tag}\n{body}".strip()


@app.post("/api/generate_song", summary="Generate a full AI song with vocals from lyrics (Bark — fully local)")
def generate_song(request: SongGenerateRequest, user=Depends(get_current_user)):
    # Validate
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Lyrics cannot be empty.")
    if len(request.lyrics) > MAX_SONG_LYRICS_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Lyrics exceed maximum length of {MAX_SONG_LYRICS_LENGTH} characters.",
        )
    if request.voice_preset not in BARK_VOICE_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice preset. Choose from: {list(BARK_VOICE_PRESETS.keys())}",
        )
    if request.style not in SONG_STYLE_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown style. Choose from: {list(SONG_STYLE_PROMPTS.keys())}",
        )
    if request.quality not in {"small", "large"}:
        raise HTTPException(status_code=400, detail="quality must be 'small' or 'large'.")

    # Load model
    import torch
    import numpy as np
    import scipy.io.wavfile
    from scipy.signal import resample_poly
    from math import gcd

    processor, bark_model = _load_bark(request.quality)
    script = _format_song_script(request.lyrics, request.style)
    voice_preset = BARK_VOICE_PRESETS[request.voice_preset]

    # ── Step 1: generate vocals with Bark ────────────────────────────────
    try:
        inputs = processor(
            text=[script], voice_preset=voice_preset, return_tensors="pt"
        ).to(_bark_device)
        with torch.no_grad():
            audio_array = bark_model.generate(**inputs, do_sample=True)
        vocals_np = audio_array.cpu().numpy().squeeze().astype(np.float32)
        vocal_sr = bark_model.generation_config.sample_rate
    except Exception as e:
        logger.error(f"Bark vocals error: {e}")
        raise HTTPException(status_code=500, detail=f"Vocal generation failed: {e}")

    # Normalise vocals
    peak = np.max(np.abs(vocals_np))
    if peak > 0:
        vocals_np = vocals_np / peak * 0.88

    # ── Step 2: generate background music with MusicGen ──────────────────
    music_np = None
    music_sr = None
    style_label = request.style if request.style != "none" else "ambient background music"
    music_prompt = f"{style_label} instrumental background music, no vocals"
    vocal_duration = len(vocals_np) / vocal_sr

    try:
        from transformers import AutoProcessor as MusicProcessor, MusicgenForConditionalGeneration
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        mg_device = "cuda" if torch.cuda.is_available() else "cpu"
        mg_proc = MusicProcessor.from_pretrained("facebook/musicgen-small")
        mg_model = MusicgenForConditionalGeneration.from_pretrained(
            "facebook/musicgen-small", torch_dtype=dtype, low_cpu_mem_usage=True
        ).to(mg_device)
        try:
            mg_inputs = mg_proc(
                text=[music_prompt], padding=True, return_tensors="pt"
            ).to(mg_device)
            mg_tokens = max(int(vocal_duration * 25.6), 128)
            mg_out = mg_model.generate(**mg_inputs, max_new_tokens=mg_tokens)
            music_np = mg_out[0, 0].cpu().numpy().astype(np.float32)
            music_sr = mg_model.config.audio_encoder.sampling_rate
        finally:
            del mg_model
            del mg_inputs
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    except Exception as e:
        logger.warning(f"Background music generation failed (vocals only): {e}")

    # ── Step 3: mix vocals + background ──────────────────────────────────
    if music_np is not None and music_sr is not None:
        # Resample music to vocal sample rate if needed
        if music_sr != vocal_sr:
            g = gcd(int(music_sr), int(vocal_sr))
            music_np = resample_poly(music_np, int(vocal_sr) // g, int(music_sr) // g).astype(np.float32)

        # Normalise background
        bg_peak = np.max(np.abs(music_np))
        if bg_peak > 0:
            music_np = music_np / bg_peak * 0.38   # background at 38% level

        # Trim or pad background to match vocal length
        n = len(vocals_np)
        if len(music_np) >= n:
            music_np = music_np[:n]
        else:
            repeats = -(-n // len(music_np))   # ceiling div
            music_np = np.tile(music_np, repeats)[:n]

        mixed = np.clip(vocals_np + music_np, -1.0, 1.0)
    else:
        mixed = vocals_np

    # Convert to int16 and stream back
    audio_int16 = (mixed * 32767).astype(np.int16)
    buf = io.BytesIO()
    scipy.io.wavfile.write(buf, rate=vocal_sr, data=audio_int16)
    audio_bytes = buf.getvalue()

    # Record history
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], "", f"bark-{request.quality}+musicgen", request.lyrics[:100], now),
    )
    conn.commit()
    conn.close()

    logger.info(f"Song generated for user {user['email']} (vocals + background)")
    return Response(content=audio_bytes, media_type="audio/wav",
                    headers={"Content-Disposition": "inline; filename=song.wav"})


@app.get("/api/song/voices", summary="List available Bark voice presets for song generation")
def list_song_voices(user=Depends(get_current_user)):
    return {
        "voices": [
            {"id": k, "bark_preset": v, "label": k.replace("_", " ").title()}
            for k, v in BARK_VOICE_PRESETS.items()
        ],
        "styles": [
            {"id": k, "label": k.replace("_", " ").title()}
            for k in SONG_STYLE_PROMPTS
        ],
    }


# ---------------------------------------------------------------------------
# Voice Cloning  (Coqui XTTS v2 — clone any voice from a 3-30 sec sample)
# ---------------------------------------------------------------------------

_xtts_model = None   # lazy-loaded
_xtts_lock = __import__("threading").Lock()

CLONE_ALLOWED_EXT   = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
CLONE_MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB
CLONE_MIN_DURATION  = 3.0                # seconds
CLONE_MAX_DURATION  = 30.0               # seconds
CLONE_MAX_PER_USER  = 10                 # max saved voice profiles

XTTS_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl",
    "tr", "ru", "nl", "cs", "ar", "zh-cn",
    "hu", "ko", "ja", "hi",
}


def _load_xtts():
    global _xtts_model
    with _xtts_lock:
        if _xtts_model is not None:
            return _xtts_model
        try:
            from TTS.api import TTS as CoquiTTS
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            _xtts_model = CoquiTTS(
                "tts_models/multilingual/multi-dataset/xtts_v2"
            ).to(device)
            logger.info(f"XTTS v2 loaded on {device}")
            return _xtts_model
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"Coqui TTS not installed: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load XTTS v2: {e}")


def _check_audio_duration(file_bytes: bytes, ext: str) -> float:
    """Return duration in seconds; raises HTTPException if invalid."""
    try:
        import soundfile as sf
        import io
        with sf.SoundFile(io.BytesIO(file_bytes)) as f:
            duration = len(f) / f.samplerate
        return duration
    except Exception:
        # Fallback: try librosa
        try:
            import librosa
            import io
            y, sr = librosa.load(io.BytesIO(file_bytes), sr=None)
            return len(y) / sr
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot read audio file: {e}")


# ── Upload a voice sample ────────────────────────────────────────────────────

@app.post("/api/voice_clone/upload", summary="Upload a voice sample to create a cloned voice profile")
@limiter.limit("10/minute")
async def upload_voice_sample(
    request: Request,
    file: UploadFile = File(..., description="Voice sample audio (3–30 sec)"),
    name: str = Form(..., description="Display name for this voice profile"),
    user=Depends(get_current_user),
):
    # Validate name
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Name must be 1–60 characters.")
    if not re.match(r'^[\w\s\-]+$', name):
        raise HTTPException(status_code=400, detail="Name may only contain letters, numbers, spaces, hyphens, and underscores.")

    # Validate file extension
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    ext = Path(file.filename).suffix.lower()
    if ext not in CLONE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {CLONE_ALLOWED_EXT}",
        )

    # Read & size-check
    content = await file.read()
    if len(content) > CLONE_MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 10 MB limit.")

    # Duration check
    duration = _check_audio_duration(content, ext)
    if duration < CLONE_MIN_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Sample too short ({duration:.1f}s). Minimum is {CLONE_MIN_DURATION}s.",
        )
    if duration > CLONE_MAX_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Sample too long ({duration:.1f}s). Maximum is {CLONE_MAX_DURATION}s.",
        )

    # Check user's profile count
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM cloned_voices WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    if count >= CLONE_MAX_PER_USER:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {CLONE_MAX_PER_USER} voice profiles reached. Delete one first.",
        )

    # Save sample file
    sample_filename = f"sample_{user['id']}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    sample_path = VOICE_SAMPLES_DIR / sample_filename
    sample_path.write_bytes(content)

    # Register in DB
    now = datetime.utcnow().isoformat()
    cursor = conn.execute(
        "INSERT INTO cloned_voices (user_id, name, reference_filename, created_at) VALUES (?,?,?,?)",
        (user["id"], name, sample_filename, now),
    )
    voice_id = cursor.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Voice sample uploaded for user {user['email']}: {sample_filename} ({duration:.1f}s)")

    return {
        "success": True,
        "voice_id": voice_id,
        "name": name,
        "duration_seconds": round(duration, 2),
        "filename": sample_filename,
        "created_at": now,
    }


# ── List saved voice profiles ────────────────────────────────────────────────

@app.get("/api/voice_clone/list", summary="List all cloned voice profiles for the current user")
def list_cloned_voices(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM cloned_voices WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {
        "voices": [dict(r) for r in rows],
        "count": len(rows),
        "limit": CLONE_MAX_PER_USER,
    }


# ── Delete a voice profile ───────────────────────────────────────────────────

@app.delete("/api/voice_clone/{voice_id}", summary="Delete a cloned voice profile")
def delete_cloned_voice(voice_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (voice_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    # Delete sample file
    sample_path = VOICE_SAMPLES_DIR / row["reference_filename"]
    if sample_path.exists():
        sample_path.unlink()

    conn.execute("DELETE FROM cloned_voices WHERE id = ?", (voice_id,))
    conn.commit()
    conn.close()

    logger.info(f"Cloned voice deleted for user {user['email']}: voice_id={voice_id}")
    return {"success": True, "deleted_id": voice_id}


# ── Generate speech with a cloned voice ─────────────────────────────────────

class VoiceCloneGenerateRequest(BaseModel):
    voice_id: int
    text: str
    language: str = "en"


@app.post("/api/voice_clone/generate", summary="Generate TTS using a cloned voice (XTTS v2)")
def generate_with_cloned_voice(
    request: VoiceCloneGenerateRequest,
    user=Depends(get_current_user),
):
    # Validate text
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds {MAX_TEXT_LENGTH} character limit.",
        )

    # Validate language
    lang = request.language.strip().lower()
    if lang not in XTTS_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{lang}'. Supported: {sorted(XTTS_LANGUAGES)}",
        )

    # All features free — no credit gate

    # Load voice profile
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (request.voice_id, user["id"]),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    sample_path = VOICE_SAMPLES_DIR / row["reference_filename"]
    if not sample_path.exists():
        raise HTTPException(status_code=404, detail="Voice sample file missing.")

    # Load XTTS model (lazy)
    tts = _load_xtts()

    # Generate
    filename = f"clone_{int(time.time())}_{secrets.token_hex(4)}.wav"
    out_path = OUTPUTS_DIR / filename

    try:
        tts.tts_to_file(
            text=request.text.strip(),
            speaker_wav=str(sample_path),
            language=lang,
            file_path=str(out_path),
        )
    except Exception as e:
        logger.error(f"XTTS generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning generation failed: {e}")

    # Deduct credits + record
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], filename, f"xtts-v2-clone:{row['name']}", request.text[:100], now),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()

    logger.info(f"Cloned voice TTS for user {user['email']}: voice='{row['name']}', file={filename}")

    return {
        "success": True,
        "filename": filename,
        "audio_url": f"/api/audio/{filename}",
        "download_url": f"/api/audio/{filename}/download",
        "voice_name": row["name"],
        "language": lang,
        "credits": updated["credits"] if not updated["is_premium"] else "Unlimited",
        "generations_remaining": user_remaining(updated),
    }




# ---------------------------------------------------------------------------
# Voice Cloning  (Coqui XTTS-v2 — fully local, no external API)
# ---------------------------------------------------------------------------

CLONED_VOICES_DIR = BASE_DIR / "cloned_voices"
CLONED_VOICES_DIR.mkdir(exist_ok=True)

MAX_REFERENCE_AUDIO_SIZE = 25 * 1024 * 1024   # 25 MB
MAX_VOICE_PROFILE_NAME_LENGTH = 50
MAX_VOICE_PROFILES_FREE = 3
MAX_VOICE_PROFILES_PREMIUM = 20
VOICE_CLONE_ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}

# XTTS-v2 supports these BCP-47 language codes
XTTS_SUPPORTED_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl", "tr",
    "ru", "nl", "cs", "ar", "zh-cn", "hu", "ko", "ja", "hi",
}

_xtts_model = None


def _load_xtts():
    """Lazy-load Coqui XTTS-v2. Raises HTTPException if unavailable."""
    global _xtts_model
    if _xtts_model is not None:
        return _xtts_model
    try:
        from TTS.api import TTS as CoquiTTS  # pip install TTS
        _xtts_model = CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2")
        logger.info("XTTS-v2 model loaded")
        return _xtts_model
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Voice cloning requires the 'TTS' package. Install with: pip install TTS",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load XTTS-v2: {e}")


# ── one-shot clone (upload reference audio + text → synthesised speech) ──

@app.post("/api/voice_clone/generate", summary="Clone a voice from an uploaded reference clip and synthesise speech")
async def voice_clone_generate(
    text: str = Form(..., description="Text to synthesise"),
    language: str = Form(default="en", description="BCP-47 language code"),
    reference_audio: UploadFile = File(..., description="Reference audio (6–30 s recommended)"),
    user=Depends(get_current_user),
):
    # All features free — no credit gate

    # Validate text
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds {MAX_TEXT_LENGTH} characters.")

    # Validate language
    language = language.strip().lower()
    if language not in XTTS_SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{language}'. Supported: {sorted(XTTS_SUPPORTED_LANGUAGES)}",
        )

    # Validate reference audio
    if not reference_audio.filename:
        raise HTTPException(status_code=400, detail="No reference audio provided.")
    ext = Path(reference_audio.filename).suffix.lower()
    if ext not in VOICE_CLONE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {VOICE_CLONE_ALLOWED_EXT}",
        )
    content = await reference_audio.read()
    if len(content) > MAX_REFERENCE_AUDIO_SIZE:
        raise HTTPException(status_code=400, detail="Reference audio exceeds 25 MB limit.")

    # Write reference to temp, synthesise to temp, stream back
    tmp_ref_path = None
    tmp_out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_ref_path = tmp.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            tmp_out_path = tmp_out.name

        tts = _load_xtts()
        tts.tts_to_file(
            text=text.strip(),
            speaker_wav=tmp_ref_path,
            language=language,
            file_path=tmp_out_path,
        )
        audio_bytes = Path(tmp_out_path).read_bytes()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice clone generate error for {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")
    finally:
        for p in (tmp_ref_path, tmp_out_path):
            if p:
                try:
                    Path(p).unlink()
                except Exception:
                    pass

    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], "", "__voice_clone__", text[:100], now),
    )
    conn.commit()
    conn.close()

    logger.info(f"Voice clone generated for {user['email']}")
    return Response(content=audio_bytes, media_type="audio/wav",
                    headers={"Content-Disposition": "inline; filename=clone.wav"})


# ── save a reusable voice profile ──

@app.post("/api/voice_clone/save_profile", summary="Save a voice profile from reference audio for repeated use")
async def save_voice_profile(
    name: str = Form(..., description="Display name for this voice profile"),
    reference_audio: UploadFile = File(..., description="Reference audio file"),
    user=Depends(get_current_user),
):
    max_profiles = MAX_VOICE_PROFILES_PREMIUM if user["is_premium"] else MAX_VOICE_PROFILES_FREE
    conn = get_db()
    existing_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM cloned_voices WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    conn.close()

    if existing_count >= max_profiles:
        detail = (
            f"Voice profile limit reached ({max_profiles}). "
            + ("Delete an existing profile to add a new one." if user["is_premium"]
               else "Upgrade to premium for more profiles.")
        )
        raise HTTPException(status_code=403, detail=detail)

    # Validate name
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Profile name cannot be empty.")
    if len(name) > MAX_VOICE_PROFILE_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Profile name exceeds {MAX_VOICE_PROFILE_NAME_LENGTH} characters.",
        )

    # Validate reference audio
    if not reference_audio.filename:
        raise HTTPException(status_code=400, detail="No reference audio provided.")
    ext = Path(reference_audio.filename).suffix.lower()
    if ext not in VOICE_CLONE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {VOICE_CLONE_ALLOWED_EXT}",
        )
    content = await reference_audio.read()
    if len(content) > MAX_REFERENCE_AUDIO_SIZE:
        raise HTTPException(status_code=400, detail="Reference audio exceeds 25 MB limit.")

    # Persist reference audio
    ref_filename = f"ref_{user['id']}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    ref_path = CLONED_VOICES_DIR / ref_filename
    ref_path.write_bytes(content)

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO cloned_voices (user_id, name, reference_filename, created_at) VALUES (?,?,?,?)",
        (user["id"], name, ref_filename, now),
    )
    profile_id = cursor.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Voice profile saved for {user['email']}: '{name}' (id={profile_id})")

    return {
        "success": True,
        "profile_id": profile_id,
        "name": name,
        "created_at": now,
    }


# ── list saved profiles ──

@app.get("/api/voice_clone/profiles", summary="List saved voice profiles for the current user")
def list_voice_profiles(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, reference_filename, created_at FROM cloned_voices "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    max_profiles = MAX_VOICE_PROFILES_PREMIUM if user["is_premium"] else MAX_VOICE_PROFILES_FREE
    return {
        "profiles": [dict(r) for r in rows],
        "count": len(rows),
        "max_profiles": max_profiles,
    }


# ── delete a saved profile ──

@app.delete("/api/voice_clone/profiles/{profile_id}", summary="Delete a saved voice profile")
def delete_voice_profile(profile_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (profile_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    # Remove stored reference audio
    ref_path = CLONED_VOICES_DIR / row["reference_filename"]
    if ref_path.exists():
        ref_path.unlink()

    conn.execute("DELETE FROM cloned_voices WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()

    logger.info(f"Voice profile {profile_id} deleted for {user['email']}")

    return {"success": True, "deleted_id": profile_id}


# ── generate from a saved profile ──

class VoiceCloneFromProfileRequest(BaseModel):
    text: str
    language: str = "en"


@app.post("/api/voice_clone/from_profile/{profile_id}", summary="Synthesise speech using a saved voice profile")
def voice_clone_from_profile(
    profile_id: int,
    request: VoiceCloneFromProfileRequest,
    user=Depends(get_current_user),
):
    # All features free — no credit gate

    # Validate text
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds {MAX_TEXT_LENGTH} characters.")

    # Validate language
    language = request.language.strip().lower()
    if language not in XTTS_SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{language}'. Supported: {sorted(XTTS_SUPPORTED_LANGUAGES)}",
        )

    # Fetch profile
    conn = get_db()
    profile = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (profile_id, user["id"]),
    ).fetchone()
    conn.close()
    if not profile:
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    ref_path = CLONED_VOICES_DIR / profile["reference_filename"]
    if not ref_path.exists():
        raise HTTPException(status_code=404, detail="Reference audio file missing from server.")

    # Synthesise to temp file, stream back
    tmp_out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            tmp_out_path = tmp_out.name
        tts = _load_xtts()
        tts.tts_to_file(
            text=request.text.strip(),
            speaker_wav=str(ref_path),
            language=language,
            file_path=tmp_out_path,
        )
        audio_bytes = Path(tmp_out_path).read_bytes()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice clone from profile error for {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")
    finally:
        if tmp_out_path:
            try:
                Path(tmp_out_path).unlink()
            except Exception:
                pass

    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], "", f"__voice_clone_profile_{profile_id}__", request.text[:100], now),
    )
    conn.commit()
    conn.close()

    logger.info(f"Voice clone from profile {profile_id} for {user['email']}")
    return Response(content=audio_bytes, media_type="audio/wav",
                    headers={"Content-Disposition": "inline; filename=clone.wav"})


# ── list supported languages ──

@app.get("/api/voice_clone/languages", summary="List languages supported by the voice cloning engine")
def voice_clone_languages(user=Depends(get_current_user)):
    return {"languages": sorted(XTTS_SUPPORTED_LANGUAGES)}


# ---------------------------------------------------------------------------
# Audio: stream, download, delete (with authorization)
# ---------------------------------------------------------------------------


@app.get("/api/audio/{filename}", summary="Stream audio file")
def get_audio(filename: str, user=Depends(get_current_user_audio)):
    """Stream audio file with authorization check."""
    
    # Validate filename
    file_path = validate_filename(filename, OUTPUTS_DIR)
    
    # Verify file ownership
    conn = get_db()
    generation = conn.execute(
        "SELECT id FROM generations WHERE filename = ? AND user_id = ?",
        (filename, user["id"])
    ).fetchone()
    conn.close()
    
    if not generation:
        raise HTTPException(status_code=403, detail="Access denied.")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    
    media_type = (
        "application/zip" if filename.endswith(".zip")
        else "audio/mpeg" if filename.endswith(".mp3")
        else "audio/wav"
    )
    return FileResponse(file_path, media_type=media_type)


@app.get("/api/audio/{filename}/download", summary="Download audio file")
def download_audio(filename: str, user=Depends(get_current_user_audio)):
    """Download audio file with authorization check."""

    # Validate filename
    file_path = validate_filename(filename, OUTPUTS_DIR)

    # Verify file ownership
    conn = get_db()
    generation = conn.execute(
        "SELECT id FROM generations WHERE filename = ? AND user_id = ?",
        (filename, user["id"])
    ).fetchone()
    conn.close()

    if not generation:
        raise HTTPException(status_code=403, detail="Access denied.")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    media_type = (
        "application/zip" if filename.endswith(".zip")
        else "audio/mpeg" if filename.endswith(".mp3")
        else "audio/wav"
    )
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.delete("/api/audio/{filename}", summary="Delete an audio file")
def delete_audio(filename: str, user=Depends(get_current_user)):
    """Delete audio file with authorization check."""
    
    # Validate filename
    file_path = validate_filename(filename, OUTPUTS_DIR)
    
    # Verify file ownership
    conn = get_db()
    generation = conn.execute(
        "SELECT id FROM generations WHERE filename = ? AND user_id = ?",
        (filename, user["id"])
    ).fetchone()
    
    if not generation:
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied.")
    
    # Delete file
    if file_path.exists():
        file_path.unlink()
    
    # Delete database record
    conn.execute("DELETE FROM generations WHERE id = ?", (generation["id"],))
    conn.commit()
    conn.close()
    
    logger.info(f"Audio file deleted for user {user['email']}: {filename}")
    
    return {"success": True, "deleted_id": generation["id"]}


@app.post("/api/auth/upgrade-to-premium", summary="Upgrade user to premium plan")
def upgrade_to_premium(user=Depends(get_current_user)):
    conn = get_db()
    conn.execute(
        "UPDATE users SET plan = ?, is_premium = ?, credits = ? WHERE id = ?",
        ("premium", True, -1, user["id"]),
    )
    conn.commit()
    conn.close()
    
    logger.info(f"User upgraded to premium: {user['email']}")
    
    return {
        "success": True,
        "message": "Successfully upgraded to premium.",
        "plan": "premium",
        "is_premium": True,
        "credits": -1,
    }


@app.get("/api/languages", summary="Get all supported languages for translation")
def get_languages():
    try:
        langs = GoogleTranslator().get_supported_languages(as_dict=True)
        return {"success": True, "languages": langs}
    except Exception as e:
        logger.error(f"Failed to retrieve languages: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve languages"
        )


_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


@app.post(
    "/api/mock-payment-webhook", summary="Mock endpoint for payment gateway webhook (dev only)"
)
def mock_payment_webhook(user_id: int, request: Request):
    if os.environ.get("ENVIRONMENT") != "development":
        raise HTTPException(status_code=404, detail="Not found.")

    secret = request.headers.get("X-Webhook-Secret", "")
    if not _WEBHOOK_SECRET or not secrets.compare_digest(secret, _WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden.")

    conn = get_db()
    user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")

    conn.execute(
        "UPDATE users SET plan = ?, is_premium = ?, credits = ? WHERE id = ?",
        ("premium", True, -1, user_id),
    )
    conn.commit()
    conn.close()

    logger.info(f"Mock payment webhook processed for user: {user_id}")

    return {
        "success": True,
        "message": f"User {user_id} upgraded to premium via mock webhook.",
    }



# ---------------------------------------------------------------------------
# Mobile / client endpoint discovery
# ---------------------------------------------------------------------------

@app.get("/api/endpoints", summary="List all API endpoints (mobile dev reference)", tags=["Info"])
def list_endpoints():
    """Returns base URL and a structured map of every endpoint — use this to configure your mobile API client."""
    endpoints = [
        # ── Auth ──────────────────────────────────────────────────────────────
        {"group": "Auth", "method": "POST", "path": "/api/auth/register",         "auth": False, "description": "Register a new account"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/login",            "auth": False, "description": "Login and receive JWT tokens"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/refresh",          "auth": False, "description": "Refresh access token using refresh_token"},
        {"group": "Auth", "method": "GET",  "path": "/api/auth/me",               "auth": True,  "description": "Get current user info"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/change-password",  "auth": True,  "description": "Change account password"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/upgrade-to-premium","auth": True, "description": "Upgrade user to premium plan"},
        # ── TTS ───────────────────────────────────────────────────────────────
        {"group": "TTS",  "method": "POST", "path": "/api/generate",              "auth": True,  "description": "Generate audio from text (rate: 20/min)"},
        {"group": "TTS",  "method": "POST", "path": "/api/batch_generate",        "auth": True,  "description": "Batch TTS → ZIP download (rate: 5/min)"},
        # ── Voices ────────────────────────────────────────────────────────────
        {"group": "Voices","method": "GET", "path": "/api/voices",                "auth": True,  "description": "All voices"},
        {"group": "Voices","method": "GET", "path": "/api/voices/free",           "auth": True,  "description": "Free-tier voices only"},
        {"group": "Voices","method": "GET", "path": "/api/voices/premium",        "auth": True,  "description": "Premium voices only"},
        # ── History ───────────────────────────────────────────────────────────
        {"group": "History","method": "GET",    "path": "/api/history",           "auth": True,  "description": "Paginated generation history (?page=1&per_page=20)"},
        {"group": "History","method": "DELETE", "path": "/api/history/{entry_id}","auth": True,  "description": "Delete a single history entry"},
        {"group": "History","method": "DELETE", "path": "/api/history",           "auth": True,  "description": "Clear all history"},
        # ── Audio files ───────────────────────────────────────────────────────
        {"group": "Audio", "method": "GET",    "path": "/api/audio/{filename}",           "auth": True, "description": "Stream audio file"},
        {"group": "Audio", "method": "GET",    "path": "/api/audio/{filename}/download",  "auth": True, "description": "Download audio file"},
        {"group": "Audio", "method": "DELETE", "path": "/api/audio/{filename}",           "auth": True, "description": "Delete audio file"},
        # ── Voice Cloning ─────────────────────────────────────────────────────
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/upload",                   "auth": True, "description": "Upload a voice sample (rate: 10/min)"},
        {"group": "VoiceClone", "method": "GET",    "path": "/api/voice_clone/list",                     "auth": True, "description": "List cloned voice profiles"},
        {"group": "VoiceClone", "method": "DELETE", "path": "/api/voice_clone/{voice_id}",               "auth": True, "description": "Delete a cloned voice profile"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/generate",                 "auth": True, "description": "Generate TTS with cloned voice (XTTS v2)"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/save_profile",             "auth": True, "description": "Save a reusable voice profile"},
        {"group": "VoiceClone", "method": "GET",    "path": "/api/voice_clone/profiles",                 "auth": True, "description": "List saved voice profiles"},
        {"group": "VoiceClone", "method": "DELETE", "path": "/api/voice_clone/profiles/{profile_id}",   "auth": True, "description": "Delete a saved voice profile"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/from_profile/{profile_id}","auth": True, "description": "Synthesise speech from a saved profile"},
        {"group": "VoiceClone", "method": "GET",    "path": "/api/voice_clone/languages",               "auth": True, "description": "Languages supported by the cloning engine"},
        # ── Audio enhancement ─────────────────────────────────────────────────
        {"group": "Audio",  "method": "POST", "path": "/api/enhance_audio",       "auth": True, "description": "Apply effects (normalize, fade, reverb, pitch, speed)"},
        {"group": "Audio",  "method": "POST", "path": "/api/transcribe",          "auth": True, "description": "Speech-to-text via local Whisper"},
        # ── Music ─────────────────────────────────────────────────────────────
        {"group": "Music",  "method": "POST", "path": "/api/generate_music",      "auth": True, "description": "Generate music locally (MusicGen)"},
        {"group": "Music",  "method": "POST", "path": "/api/generate_music_fal",  "auth": True, "description": "Generate music via FAL.AI stable-audio"},
        # ── Song generation ───────────────────────────────────────────────────
        {"group": "Song",   "method": "POST", "path": "/api/generate_song",       "auth": True, "description": "Generate AI song with vocals (Bark, fully local)"},
        {"group": "Song",   "method": "GET",  "path": "/api/song/voices",         "auth": True, "description": "List Bark voice presets"},
        # ── Translation ───────────────────────────────────────────────────────
        {"group": "Translation", "method": "POST", "path": "/api/translate",      "auth": True, "description": "Translate text to target language"},
        {"group": "Translation", "method": "GET",  "path": "/api/languages",      "auth": False, "description": "List all supported translation languages"},
        # ── Stats ─────────────────────────────────────────────────────────────
        {"group": "Stats",  "method": "GET",  "path": "/api/stats",               "auth": True, "description": "User usage statistics"},
    ]

    for ep in endpoints:
        ep["url"] = BASE_URL.rstrip("/") + ep["path"]

    return {
        "base_url": BASE_URL,
        "docs_url": BASE_URL.rstrip("/") + "/docs",
        "openapi_url": BASE_URL.rstrip("/") + "/openapi.json",
        "auth_header": "Authorization: Bearer <access_token>",
        "total": len(endpoints),
        "endpoints": endpoints,
    }


# ---------------------------------------------------------------------------
# Static file serving (must be mounted after all API routes)
# ---------------------------------------------------------------------------

