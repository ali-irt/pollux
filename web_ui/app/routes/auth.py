"""
app/routes/auth.py
Authentication endpoints: register, login, refresh, me, change_password,
upgrade-to-premium, profile, and mock-payment-webhook.
"""
import logging
import os
import secrets

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import INITIAL_FREE_CREDITS, _WEBHOOK_SECRET
from app.security import (
    create_token,
    create_refresh_token,
    hash_password,
    verify_password,
    validate_password_strength,
    validate_and_normalize_email,
    record_failed_login_attempt,
    is_account_locked,
    clear_login_attempts,
    get_current_user,
    user_remaining,
)
import jwt
from app.config import JWT_SECRET, JWT_ALGORITHM
from db import get_db

logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/auth/register", summary="Register a new account")
@limiter.limit("5/minute")
def register(req: RegisterRequest, request: Request):
    try:
        req.email = validate_and_normalize_email(req.email)
    except HTTPException:
        raise

    if not req.password:
        raise HTTPException(status_code=400, detail="Password is required.")

    is_valid, error_msg = validate_password_strength(req.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    conn = get_db()
    if conn.execute(
        "SELECT id FROM users WHERE email = ?", (req.email,)
    ).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Email already registered.")

    now = datetime.utcnow().isoformat()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, plan, generation_count, credits, is_premium, created_at)"
        " VALUES (?, ?, 'free', 0, ?, ?, ?) RETURNING id",
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


@router.post("/api/auth/login", summary="Login and receive JWT tokens")
@limiter.limit("10/minute")
def login(req: LoginRequest, request: Request):
    is_locked, msg = is_account_locked(req.email)
    if is_locked:
        logger.warning(f"Blocked login attempt for locked account: {req.email}")
        raise HTTPException(status_code=429, detail=msg or "Account temporarily locked. Try again later.")

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?", (req.email.lower(),)
    ).fetchone()
    conn.close()

    if not user or not verify_password(req.password, user["password_hash"]):
        ip_address = request.client.host if request.client else "unknown"
        record_failed_login_attempt(req.email, ip_address)
        logger.warning(f"Failed login attempt for: {req.email}")
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    clear_login_attempts(req.email)

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


@router.post("/api/auth/refresh", summary="Refresh access token")
def refresh_access_token(req: RefreshTokenRequest):
    try:
        payload = jwt.decode(
            req.refresh_token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": True}
        )

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

    conn = get_db()
    user = conn.execute(
        "SELECT id, email FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="User not found.")

    new_access_token = create_token(user["id"], user["email"])

    return {"access_token": new_access_token, "token_type": "bearer"}


@router.get("/api/auth/me", summary="Get current user info")
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


@router.post("/api/auth/change-password", summary="Change account password")
def change_password(req: ChangePasswordRequest, user=Depends(get_current_user)):
    current_password = req.current_password.strip()
    new_password = req.new_password.strip()

    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    if verify_password(new_password, user["password_hash"]):
        raise HTTPException(
            status_code=400,
            detail="New password cannot be the same as the current password."
        )

    is_valid, error_msg = validate_password_strength(new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user["id"]),
    )
    conn.commit()
    conn.close()

    logger.info(f"Password changed for user: {user['email']}")

    return {"success": True, "message": "Password updated successfully."}


@router.post("/api/auth/upgrade-to-premium", summary="Upgrade user to premium plan")
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


@router.post(
    "/api/mock-payment-webhook", summary="Mock endpoint for payment gateway webhook (dev only)"
)
def mock_payment_webhook(user_id: int, request: Request):
    if os.environ.get("ENVIRONMENT", "production") != "development":
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
