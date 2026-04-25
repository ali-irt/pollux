"""
app/security.py
JWT helpers, password hashing/verification, FastAPI auth dependencies.
"""
import hashlib
import re
import logging

import bcrypt as _bcrypt
import jwt
from datetime import datetime, timedelta

from fastapi import HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from email_validator import validate_email, EmailNotValidError

from app.config import (
    JWT_SECRET, JWT_ALGORITHM,
    MAX_EMAIL_LENGTH, MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH,
    CREDITS_PER_GENERATION,
)
from db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI security schemes
# ---------------------------------------------------------------------------

security = HTTPBearer()
security_optional = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("$2b$") or stored.startswith("$2a$"):
            return _bcrypt.checkpw(password.encode(), stored.encode())
        # Legacy SHA-256 format: "salt:hash" — accept but do not upgrade here
        salt, hashed = stored.split(":", 1)
        return hashlib.sha256((password + salt).encode()).hexdigest() == hashed
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Password validation
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
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."

    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"Password must not exceed {MAX_PASSWORD_LENGTH} characters."

    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter (A-Z)."

    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter (a-z)."

    if not re.search(r'\d', password):
        return False, "Password must contain at least one number (0-9)."

    special_chars = r'[!@#$%^&*()_+=\-\[\]{};:\'",.<>?/\\|`~]'
    if not re.search(special_chars, password):
        return False, "Password must contain at least one special character (!@#$%^&*...)."

    return True, "Valid"


# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------


def validate_and_normalize_email(email: str) -> str:
    """
    Validate and normalize email address.

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
# Failed login tracking
# ---------------------------------------------------------------------------


def record_failed_login_attempt(email: str, ip_address: str):
    """Record failed login attempt in database."""
    conn = get_db()

    conn.execute(
        "INSERT INTO failed_login_attempts (email, attempt_time, ip_address) VALUES (?, ?, ?)",
        (email.lower(), datetime.utcnow().isoformat(), ip_address)
    )

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
# JWT token creation
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


# ---------------------------------------------------------------------------
# FastAPI dependency: get current authenticated user
# ---------------------------------------------------------------------------


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

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="User not found.")

    return dict(user)


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


# ---------------------------------------------------------------------------
# Credits / plan helpers
# ---------------------------------------------------------------------------


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
            "message": (
                f"You have {user['credits']} credits. "
                f"Each generation costs {CREDITS_PER_GENERATION} credits. "
                "Please top up or upgrade to continue."
            ),
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
