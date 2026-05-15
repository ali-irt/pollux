"""
web_ui/main.py
Thin entry point — creates the FastAPI app, registers middleware, includes
all routers, runs init_db() at startup, and pre-warms models in background
threads.

Run with:  uvicorn main:app  (from the web_ui/ directory)
"""
import logging
import os
import threading
import time

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import ALLOWED_ORIGINS, BASE_DIR
from app.ai.loaders import (
    _prefetch_ace_step_model,
    _prefetch_xtts_model,
    _prefetch_whisper_model,
)
from app.routes import (
    auth,
    tts,
    voices,
    history,
    audio,
    music,
    transcribe,
    translate,
    enhance,
    stats,
    voxcpm,
)
from db import init_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan (startup logic)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    try:
        import torch
        n = os.cpu_count() or 4
        torch.set_num_threads(n)
        torch.set_num_interop_threads(max(2, n // 2))
        logger.info(f"Torch CPU threads set to {n}")
    except Exception:
        pass
    threading.Thread(target=_prefetch_ace_step_model, daemon=True).start()
    threading.Thread(target=_prefetch_xtts_model, daemon=True).start()
    threading.Thread(target=_prefetch_whisper_model, daemon=True).start()

    def _warmup_voxcpm():
        from app.voice_cloning_client import is_available
        if is_available():
            logger.info("VoxCPM voice cloning service is reachable")
        else:
            logger.warning("VoxCPM voice cloning service is NOT reachable at startup")

    threading.Thread(target=_warmup_voxcpm, daemon=True).start()
    yield

    # Graceful shutdown: close DB pool and mark orphaned jobs as failed
    try:
        from db.database import _pool
        from db import get_db
        conn = get_db()
        conn.execute(
            "UPDATE jobs SET status='failed', error_message='Server shutdown'"
            " WHERE status IN ('pending','processing')"
        )
        conn.commit()
        conn.close()
        if _pool:
            _pool.closeall()
        logger.info("DB pool closed on shutdown")
    except Exception as exc:
        logger.warning("Shutdown cleanup error: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="VoiceWave", version="2.0.0", lifespan=lifespan)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    logger.info(
        f"{request.method} {request.url.path} - "
        f"Status: {response.status_code} - "
        f"Duration: {duration:.3f}s - "
        f"IP: {request.client.host if request.client else 'unknown'}"
    )
    return response

# ---------------------------------------------------------------------------
# Rate limit exception handler
# ---------------------------------------------------------------------------


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    logger.warning(f"Rate limit exceeded for IP: {request.client.host if request.client else 'unknown'}")
    return JSONResponse(status_code=429, content={"detail": "Too many requests. Please try again later."})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    def _safe(obj):
        if isinstance(obj, bytes):
            return f"<binary {len(obj)} bytes>"
        if isinstance(obj, dict):
            return {k: _safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_safe(i) for i in obj]
        return obj
    return JSONResponse(status_code=422, content={"detail": _safe(exc.errors())})


# ---------------------------------------------------------------------------
# Health check (no auth — used by load balancers, Docker, k8s)
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health():
    try:
        from db import get_db
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return {"status": "ok", "db": "ok"}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "degraded", "db": str(exc)})


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(tts.router)
app.include_router(voices.router)
app.include_router(history.router)
app.include_router(audio.router)
app.include_router(music.router)
app.include_router(transcribe.router)
app.include_router(translate.router)
app.include_router(enhance.router)
app.include_router(stats.router)
app.include_router(voxcpm.router)

# ---------------------------------------------------------------------------
# Static files (must be mounted after all API routes)
# ---------------------------------------------------------------------------

_static_dir = BASE_DIR / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

