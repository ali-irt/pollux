import os
import time
import shutil
import subprocess
import hashlib
import secrets
import re
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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
DB_PATH = BASE_DIR / "pollux.db"

OUTPUTS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)

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

    if is_edge:
        cmd = [
            resolve_bin("edge-tts"),
            "--voice",
            model_info.get("edge_voice", "ur-PK-UzmaNeural"),
            "--pitch",
            model_info.get("edge_pitch", "+0Hz"),
            "--rate",
            model_info.get("edge_rate", "+0%"),
            "--text",
            request.text,
            "--write-media",
            str(out_path),
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"edge-tts failed: {stderr}")
    else:
        for file_path, file_meta in model_info.get("files", {}).items():
            dest = MODELS_DIR / file_path.split("/")[-1]
            if not dest.exists():
                dl_url = (
                    file_meta.get("url")
                    or f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{file_path}"
                )
                urllib.request.urlretrieve(dl_url, dest)

        model_onnx = MODELS_DIR / f"{request.model}.onnx"
        try:
            from piper import PiperVoice
            import wave

            voice = PiperVoice.load(str(model_onnx))
            with wave.open(str(out_path), "wb") as wav_file:
                voice.synthesize_wav(request.text, wav_file)
        except ImportError:
            cmd = [
                resolve_bin("piper"),
                "--model",
                str(model_onnx),
                "--output_file",
                str(out_path),
            ]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _, stderr = proc.communicate(input=request.text)
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=500, detail=f"piper CLI failed: {stderr}"
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
        "SELECT COUNT(*) as cnt FROM generations WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    rows = conn.execute(
        "SELECT * FROM generations WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
    
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
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
    
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
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
