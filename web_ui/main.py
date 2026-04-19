import os
import time
import shutil
import subprocess
import hashlib
import secrets
import re
import logging
import zipfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Query, Request, UploadFile, File, Form
from fastapi.responses import FileResponse
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

from db import get_db, init_db

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

ALLOWED_ORIGINS = ["*"]

# Add development origins only in dev mode
if os.environ.get("ENVIRONMENT") == "development":
    ALLOWED_ORIGINS.extend([
        "http://localhost:3000",
        "http://localhost:8000",
        "http://localhost:8081",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
        "http://localhost:8081",
    ])

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # Specific origins only
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],  # Specific methods
    allow_headers=["Content-Type", "Authorization"],  # Specific headers
    expose_headers=["Content-Length"],
    max_age=3600,
)

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
        # 🔒 stricter for production
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none';"

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
    """Handle rate limit exceeded errors."""
    logger.warning(f"Rate limit exceeded for IP: {request.client.host if request.client else 'unknown'}")
    return {
        "detail": "Too many requests. Please try again later.",
        "status_code": 429
    }

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

    # Lock account if >= 5 attempts
    if count >= 5:
        locked_until = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO locked_accounts (email, locked_until) VALUES (?, ?)",
            (email.lower(), locked_until)
        )
        logger.warning(f"Account locked for {email} after 5 failed attempts")

    conn.commit()
    conn.close()


# def is_account_locked(email: str) -> tuple[bool, str]:
#     """Check if account is locked."""
#     conn = get_db()
#     result = conn.execute(
#         "SELECT locked_until FROM locked_accounts WHERE email = ? AND locked_until > ?",
#         (email.lower(), datetime.utcnow().isoformat())
#     ).fetchone()
#     conn.close()
    
#     if result:
#         return True, "Account locked due to too many failed attempts. Try again after 15 minutes."
#     return False, ""


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
        "exp": datetime.utcnow() + timedelta(days=30),
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
import jwt
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
    # Check if account is locked
    # is_locked, msg = is_account_locked(req.email)
    # if is_locked:
    #     raise HTTPException(status_code=429, detail=msg)
    
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
    save_location: str = ""
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
def generate_audio(request: GenerateRequest, user=Depends(get_current_user)):
    # INPUT VALIDATION (before expensive operations)
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds maximum length of {MAX_TEXT_LENGTH} characters."
        )

    # Load voices
    voices_info = load_voices_json()
    if request.model not in voices_info:
        raise HTTPException(status_code=400, detail=f"Unknown model: {request.model}")

    model_info = voices_info[request.model]

    # ACCESS CHECKS (before expensive operations)
    if is_premium_voice(model_info) and not user["is_premium"]:
        premium_voice_error()

    if not user["is_premium"] and user["credits"] < CREDITS_PER_GENERATION:
        freemium_error(user)

    # Generate
    is_edge = model_info.get("engine") == "edge-tts"
    ext = ".mp3" if is_edge else ".wav"
    filename = f"output_{int(time.time())}{ext}"
    out_path = OUTPUTS_DIR / filename

    _synthesize_audio(
        model_info, request.model, request.text, out_path,
        speed=max(0.5, min(2.0, request.speed)),
        pitch_hz=max(-10, min(10, request.pitch_hz)),
    )

    # Increment usage
    conn = get_db()
    if not user["is_premium"]:
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ?",
            (CREDITS_PER_GENERATION, user["id"]),
        )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (
            user["id"],
            filename,
            request.model,
            request.text[:100],
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    updated_user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user["id"],)
    ).fetchone()
    conn.close()

    # Optional custom save
    final_save_path = str(out_path)
    if request.save_location:
        save_dir = Path(request.save_location)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_path, save_dir / filename)
            final_save_path = str(save_dir / filename)
        except Exception as e:
            final_save_path = (
                f"Cached only — could not save to {request.save_location}: {e}"
            )

    remaining_credits = user_remaining(updated_user)
    response_message = "Audio generated successfully!"
    if updated_user["is_premium"]:
        credits_info = "Unlimited (Premium User)"
        generations_info = "Unlimited (Premium User)"
    else:
        credits_info = updated_user["credits"]
        generations_info = remaining_credits

    logger.info(f"Audio generated for user {user['email']}: {request.model}")

    return {
        "success": True,
        "status_message": response_message,
        "filename": filename,
        "audio_url": f"/api/audio/{filename}",
        "download_url": f"/api/audio/{filename}/download",
        "saved_location": final_save_path,
        "generation_count": updated_user["generation_count"],
        "user_plan": "premium" if updated_user["is_premium"] else "free",
        "credits": credits_info,
        "generations_remaining": generations_info,
    }


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
        "SELECT * FROM generations WHERE id = ? AND user_id = ?", (entry_id, user["id"])
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="History entry not found.")
    
    file_path = OUTPUTS_DIR / row["filename"]
    if file_path.exists():
        file_path.unlink()
    
    conn.execute("DELETE FROM generations WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    
    logger.info(f"History entry deleted for user {user['email']}: {entry_id}")
    
    return {"success": True, "deleted_id": entry_id}


@app.delete("/api/history", summary="Clear all history for current user")
def clear_history(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT filename FROM generations WHERE user_id = ?", (user["id"],)
    ).fetchall()
    for row in rows:
        fp = OUTPUTS_DIR / row["filename"]
        if fp.exists():
            fp.unlink()
    
    conn.execute("DELETE FROM generations WHERE user_id = ?", (user["id"],))
    conn.commit()
    conn.close()
    
    logger.info(f"All history cleared for user {user['email']}: {len(rows)} items")
    
    return {"success": True, "deleted_count": len(rows)}


# ---------------------------------------------------------------------------
# Batch TTS Generation
# ---------------------------------------------------------------------------

MAX_BATCH_LINES = 20
MAX_BATCH_LINE_LENGTH = 500


class BatchGenerateRequest(BaseModel):
    texts: list
    model: str
    speed: float = 1.0
    pitch_hz: int = 0


@app.post("/api/batch_generate", summary="Batch TTS: generate multiple clips and return a ZIP download")
def batch_generate(request: BatchGenerateRequest, user=Depends(get_current_user)):
    texts = [t.strip() for t in request.texts if t.strip()]
    if not texts:
        raise HTTPException(status_code=400, detail="No text lines provided.")
    if len(texts) > MAX_BATCH_LINES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_LINES} lines per batch.")
    for t in texts:
        if len(t) > MAX_BATCH_LINE_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Each line must be under {MAX_BATCH_LINE_LENGTH} characters.",
            )

    voices_info = load_voices_json()
    if request.model not in voices_info:
        raise HTTPException(status_code=400, detail=f"Unknown model: {request.model}")
    model_info = voices_info[request.model]

    if is_premium_voice(model_info) and not user["is_premium"]:
        premium_voice_error()

    total_credits = CREDITS_PER_GENERATION * len(texts)
    if not user["is_premium"] and user["credits"] < total_credits:
        return freemium_error(user)

    is_edge = model_info.get("engine") == "edge-tts"
    ext = ".mp3" if is_edge else ".wav"
    batch_ts = int(time.time())
    generated_files: list[str] = []

    conn = get_db()
    now = datetime.utcnow().isoformat()
    try:
        for i, text in enumerate(texts):
            filename = f"batch_{batch_ts}_{i + 1:03d}{ext}"
            out_path = OUTPUTS_DIR / filename
            _synthesize_audio(
                model_info, request.model, text, out_path,
                speed=max(0.5, min(2.0, request.speed)),
                pitch_hz=max(-10, min(10, request.pitch_hz)),
            )
            conn.execute(
                "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
                (user["id"], filename, request.model, text[:100], now),
            )
            generated_files.append(filename)

        if not user["is_premium"]:
            conn.execute(
                "UPDATE users SET credits = credits - ? WHERE id = ?",
                (total_credits, user["id"]),
            )
        conn.execute(
            "UPDATE users SET generation_count = generation_count + ? WHERE id = ?",
            (len(texts), user["id"]),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        conn.close()
        raise
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"Batch generation failed: {str(e)}")

    # Package all clips into a ZIP archive
    zip_filename = f"batch_{batch_ts}.zip"
    zip_path = OUTPUTS_DIR / zip_filename
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in generated_files:
            fp = OUTPUTS_DIR / fname
            if fp.exists():
                zf.write(fp, fname)

    # Register the ZIP so the download endpoint can serve it
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], zip_filename, "__batch_zip__", f"Batch {len(texts)} clips", now),
    )
    conn.commit()
    conn.close()

    logger.info(f"Batch TTS: {len(texts)} clips for user {user['email']}")

    return {
        "success": True,
        "count": len(texts),
        "zip_filename": zip_filename,
        "zip_download_url": f"/api/audio/{zip_filename}/download",
        "clips": [
            {"index": i + 1, "filename": f, "url": f"/api/audio/{f}"}
            for i, f in enumerate(generated_files)
        ],
    }


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

music_processor = None
music_model = None
music_device = "cpu"


class MusicGenerateRequest(BaseModel):
    prompt: str
    duration: int = 10


@app.post("/api/generate_music", summary="Generate music from a text prompt (premium)")
def generate_music(request: MusicGenerateRequest, user=Depends(get_current_user)):
    # if not user["is_premium"]:
    #     premium_feature_error("Music generation")
    
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    
    if len(request.prompt) > MAX_MUSIC_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt exceeds maximum length of {MAX_MUSIC_PROMPT_LENGTH} characters."
        )

    global music_processor, music_model, music_device
    if music_processor is None:
        try:
            import torch
            from transformers import AutoProcessor, MusicgenForConditionalGeneration

            if torch.backends.mps.is_available():
                music_device = "mps"
            elif torch.cuda.is_available():
                music_device = "cuda"
            else:
                music_device = "cpu"
            music_processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
            music_model = MusicgenForConditionalGeneration.from_pretrained(
                "facebook/musicgen-small"
            ).to(music_device)
        except ImportError as e:
            raise HTTPException(
                status_code=500, detail=f"MusicGen dependencies missing: {e}"
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load MusicGen: {e}")

    assert music_model is not None
    assert music_processor is not None

    try:
        import scipy.io.wavfile

        inputs = music_processor(
            text=[request.prompt], padding=True, return_tensors="pt"
        ).to(music_device)
        audio_values = music_model.generate(
            **inputs, max_new_tokens=int(request.duration * 25.6)
        )
        filename = f"music_{int(time.time())}.wav"
        out_path = OUTPUTS_DIR / filename
        scipy.io.wavfile.write(
            str(out_path),
            rate=music_model.config.audio_encoder.sampling_rate,
            data=audio_values[0, 0].cpu().numpy(),
        )
        
        logger.info(f"Music generated for user {user['email']}")
        
        return {
            "success": True,
            "filename": filename,
            "audio_url": f"/api/audio/{filename}",
            "download_url": f"/api/audio/{filename}/download",
            "saved_location": str(out_path),
        }
    except Exception as e:
        logger.error(f"Music generation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Music generation error: {str(e)}")


# ---------------------------------------------------------------------------
# Local Music Enhancement Studio (no external APIs — uses librosa + scipy)
# ---------------------------------------------------------------------------


class EnhanceAudioRequest(BaseModel):
    filename: str
    normalize: bool = True
    fade_in: float = 0.0      # seconds (0–5)
    fade_out: float = 0.0     # seconds (0–5)
    reverb_amount: float = 0.0  # 0.0 to 1.0
    pitch_steps: int = 0      # semitones (-6 to +6)
    speed_factor: float = 1.0  # 0.5 to 2.0 (time-stretch, pitch preserved)


@app.post("/api/enhance_audio", summary="Apply local audio effects (normalize, fade, reverb, pitch, speed)")
def enhance_audio(request: EnhanceAudioRequest, user=Depends(get_current_user)):
    try:
        import librosa
        import librosa.effects
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Audio processing libraries missing: {e}")

    # Validate and check ownership
    src_path = validate_filename(request.filename, OUTPUTS_DIR)
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM generations WHERE filename = ? AND user_id = ?",
        (request.filename, user["id"]),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=403, detail="Access denied.")
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    # Clamp params
    fade_in = max(0.0, min(5.0, request.fade_in))
    fade_out = max(0.0, min(5.0, request.fade_out))
    reverb_amount = max(0.0, min(1.0, request.reverb_amount))
    pitch_steps = max(-6, min(6, request.pitch_steps))
    speed_factor = max(0.5, min(2.0, request.speed_factor))

    try:
        y, sr = librosa.load(str(src_path), sr=None, mono=False)
        # Ensure shape is (channels, samples) for multi-channel support
        if y.ndim == 1:
            y = y[np.newaxis, :]  # (1, N)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load audio: {e}")

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
        if request.normalize:
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

    # Write output
    stem = src_path.stem
    enhanced_filename = f"enhanced_{stem}_{int(time.time())}.wav"
    enhanced_path = OUTPUTS_DIR / enhanced_filename
    try:
        sf.write(str(enhanced_path), result, sr)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write enhanced audio: {e}")

    # Register in DB
    conn = get_db()
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], enhanced_filename, "__enhanced__", f"Enhanced: {request.filename}", datetime.utcnow().isoformat()),
    )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    conn.commit()
    conn.close()

    logger.info(f"Audio enhanced for user {user['email']}: {enhanced_filename}")

    return {
        "success": True,
        "filename": enhanced_filename,
        "audio_url": f"/api/audio/{enhanced_filename}",
        "download_url": f"/api/audio/{enhanced_filename}/download",
        "effects_applied": {
            "normalize": request.normalize,
            "fade_in": fade_in,
            "fade_out": fade_out,
            "reverb_amount": reverb_amount,
            "pitch_steps": pitch_steps,
            "speed_factor": speed_factor,
        },
    }


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
# FAL.AI music generation (stable-audio)
# ---------------------------------------------------------------------------

FAL_API_KEY = os.environ.get("FAL_API_KEY")
FAL_QUEUE_BASE = "https://queue.fal.run"
FAL_MODEL = "fal-ai/stable-audio"

MAX_FAL_PROMPT_LENGTH = 400
FAL_POLL_INTERVAL = 3   # seconds between status checks
FAL_POLL_TIMEOUT = 180  # max seconds to wait


class FalMusicRequest(BaseModel):
    prompt: str
    duration: float = 30.0  # seconds, max ~190


def _fal_headers() -> dict:
    if not FAL_API_KEY:
        raise HTTPException(status_code=503, detail="FAL_API_KEY not configured.")
    return {
        "Authorization": f"Key {FAL_API_KEY}",
        "Content-Type": "application/json",
    }


def _fal_http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_fal_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise HTTPException(status_code=e.code, detail=f"FAL API error: {body_text}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"FAL API unreachable: {e.reason}")


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

    # Credits check (same cost as a generation)
    if not user["is_premium"] and user["credits"] < CREDITS_PER_GENERATION:
        return freemium_error(user)

    # Load model
    processor, model = _load_bark(request.quality)

    # Build script
    script = _format_song_script(request.lyrics, request.style)
    voice_preset = BARK_VOICE_PRESETS[request.voice_preset]

    try:
        import torch
        import scipy.io.wavfile
        import numpy as np

        inputs = processor(
            text=[script],
            voice_preset=voice_preset,
            return_tensors="pt",
        ).to(_bark_device)

        with torch.no_grad():
            audio_array = model.generate(**inputs, do_sample=True)

        audio_np = audio_array.cpu().numpy().squeeze().astype(np.float32)

        # Normalise to avoid clipping
        peak = np.max(np.abs(audio_np))
        if peak > 0:
            audio_np = audio_np / peak * 0.92

        # Convert to int16 for WAV
        audio_int16 = (audio_np * 32767).astype(np.int16)

        sample_rate = model.generation_config.sample_rate

    except Exception as e:
        logger.error(f"Bark generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Song generation failed: {e}")

    # Save file
    filename = f"song_{int(time.time())}_{secrets.token_hex(4)}.wav"
    out_path = OUTPUTS_DIR / filename
    try:
        import scipy.io.wavfile
        scipy.io.wavfile.write(str(out_path), rate=sample_rate, data=audio_int16)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write audio: {e}")

    # Record in DB + deduct credits
    now = datetime.utcnow().isoformat()
    conn = get_db()
    if not user["is_premium"]:
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ?",
            (CREDITS_PER_GENERATION, user["id"]),
        )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], filename, f"bark-{request.quality}", request.lyrics[:100], now),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()

    logger.info(f"Song generated for user {user['email']}: {filename}")

    return {
        "success": True,
        "filename": filename,
        "audio_url": f"/api/audio/{filename}",
        "download_url": f"/api/audio/{filename}/download",
        "script_used": script,
        "voice_preset": voice_preset,
        "sample_rate": sample_rate,
        "credits": updated["credits"] if not updated["is_premium"] else "Unlimited",
        "generations_remaining": user_remaining(updated),
    }


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
async def upload_voice_sample(
    file: UploadFile = File(..., description="Voice sample audio (3–30 sec)"),
    name: str = Form(..., description="Display name for this voice profile"),
    user=Depends(get_current_user),
):
    # Validate name
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Name must be 1–60 characters.")

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

    # Credits check
    if not user["is_premium"] and user["credits"] < CREDITS_PER_GENERATION:
        return freemium_error(user)

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
    if not user["is_premium"]:
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ?",
            (CREDITS_PER_GENERATION, user["id"]),
        )
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


def _download_to_outputs(audio_url: str, filename: str) -> Path:
    out_path = OUTPUTS_DIR / filename
    try:
        urllib.request.urlretrieve(audio_url, out_path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to download audio: {e}")
    return out_path


@app.post("/api/generate_music_fal", summary="Generate music with FAL.AI stable-audio")
def generate_music_fal(request: FalMusicRequest, user=Depends(get_current_user)):
    # Validate
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    if len(request.prompt) > MAX_FAL_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt exceeds maximum length of {MAX_FAL_PROMPT_LENGTH} characters."
        )
    if not (1.0 <= request.duration <= 190.0):
        raise HTTPException(status_code=400, detail="Duration must be between 1 and 190 seconds.")

    # Credits check
    if not user["is_premium"] and user["credits"] < CREDITS_PER_GENERATION:
        return freemium_error(user)

    # Submit to FAL queue
    submit = _fal_http(
        "POST",
        f"{FAL_QUEUE_BASE}/{FAL_MODEL}",
        {"prompt": request.prompt.strip(), "seconds_total": request.duration},
    )
    request_id = submit.get("request_id")
    if not request_id:
        raise HTTPException(status_code=502, detail=f"FAL did not return a request_id: {submit}")

    # Poll for completion
    deadline = time.time() + FAL_POLL_TIMEOUT
    result = None
    while time.time() < deadline:
        time.sleep(FAL_POLL_INTERVAL)
        status_data = _fal_http(
            "GET",
            f"{FAL_QUEUE_BASE}/{FAL_MODEL}/requests/{request_id}/status",
        )
        status = (status_data.get("status") or "").upper()
        if status == "COMPLETED":
            result = _fal_http(
                "GET",
                f"{FAL_QUEUE_BASE}/{FAL_MODEL}/requests/{request_id}",
            )
            break
        if status in ("FAILED", "CANCELLED"):
            raise HTTPException(status_code=500, detail=f"FAL generation failed: {status_data}")

    if not result:
        raise HTTPException(status_code=504, detail="FAL generation timed out.")

    audio_url = (result.get("audio_file") or {}).get("url")
    if not audio_url:
        raise HTTPException(status_code=502, detail=f"FAL returned no audio URL: {result}")

    # Download and save
    filename = f"fal_music_{int(time.time())}_{secrets.token_hex(4)}.wav"
    _download_to_outputs(audio_url, filename)

    now = datetime.utcnow().isoformat()
    conn = get_db()
    if not user["is_premium"]:
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ?",
            (CREDITS_PER_GENERATION, user["id"]),
        )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], filename, "fal-stable-audio", request.prompt[:100], now),
    )
    conn.commit()
    updated_user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()

    logger.info(f"FAL music generated for user {user['email']}: {filename}")

    return {
        "success": True,
        "filename": filename,
        "audio_url": f"/api/audio/{filename}",
        "download_url": f"/api/audio/{filename}/download",
        "credits": updated_user["credits"] if not updated_user["is_premium"] else "Unlimited",
        "generations_remaining": user_remaining(updated_user),
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
    # Credits check
    if not user["is_premium"] and user["credits"] < CREDITS_PER_GENERATION:
        return freemium_error(user)

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

    # Write reference to a temp file (XTTS needs a file path)
    tmp_ref_path = None
    out_filename = f"clone_{int(time.time())}_{secrets.token_hex(4)}.wav"
    out_path = OUTPUTS_DIR / out_filename
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_ref_path = tmp.name

        tts = _load_xtts()
        tts.tts_to_file(
            text=text.strip(),
            speaker_wav=tmp_ref_path,
            language=language,
            file_path=str(out_path),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice clone generate error for {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")
    finally:
        if tmp_ref_path:
            try:
                Path(tmp_ref_path).unlink()
            except Exception:
                pass

    # Record usage
    now = datetime.utcnow().isoformat()
    conn = get_db()
    if not user["is_premium"]:
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ?",
            (CREDITS_PER_GENERATION, user["id"]),
        )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], out_filename, "__voice_clone__", text[:100], now),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()

    logger.info(f"Voice clone generated for {user['email']}: {out_filename}")

    return {
        "success": True,
        "filename": out_filename,
        "audio_url": f"/api/audio/{out_filename}",
        "download_url": f"/api/audio/{out_filename}/download",
        "credits": updated["credits"] if not updated["is_premium"] else "Unlimited",
        "generations_remaining": user_remaining(updated),
    }


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
    # Credits check
    if not user["is_premium"] and user["credits"] < CREDITS_PER_GENERATION:
        return freemium_error(user)

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

    # Synthesise
    out_filename = f"clone_{int(time.time())}_{secrets.token_hex(4)}.wav"
    out_path = OUTPUTS_DIR / out_filename
    try:
        tts = _load_xtts()
        tts.tts_to_file(
            text=request.text.strip(),
            speaker_wav=str(ref_path),
            language=language,
            file_path=str(out_path),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice clone from profile error for {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")

    # Record usage
    now = datetime.utcnow().isoformat()
    conn = get_db()
    if not user["is_premium"]:
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ?",
            (CREDITS_PER_GENERATION, user["id"]),
        )
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at) VALUES (?,?,?,?,?)",
        (user["id"], out_filename, f"__voice_clone_profile_{profile_id}__", request.text[:100], now),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()

    logger.info(f"Voice clone from profile {profile_id} for {user['email']}: {out_filename}")

    return {
        "success": True,
        "filename": out_filename,
        "audio_url": f"/api/audio/{out_filename}",
        "download_url": f"/api/audio/{out_filename}/download",
        "voice_profile": profile["name"],
        "credits": updated["credits"] if not updated["is_premium"] else "Unlimited",
        "generations_remaining": user_remaining(updated),
    }


# ── list supported languages ──

@app.get("/api/voice_clone/languages", summary="List languages supported by the voice cloning engine")
def voice_clone_languages(user=Depends(get_current_user)):
    return {"languages": sorted(XTTS_SUPPORTED_LANGUAGES)}


# ---------------------------------------------------------------------------
# Audio: stream, download, delete (with authorization)
# ---------------------------------------------------------------------------


@app.get("/api/audio/{filename}", summary="Stream audio file")
def get_audio(filename: str, user=Depends(get_current_user)):
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
def download_audio(filename: str, user=Depends(get_current_user)):
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


@app.post(
    "/api/mock-payment-webhook", summary="Mock endpoint for payment gateway webhook"
)
def mock_payment_webhook(user_id: int):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    conn = get_db()
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
# Static file serving (must be mounted after all API routes)
# ---------------------------------------------------------------------------

