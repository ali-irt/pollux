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
    _prefetch_music_model,
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
    voice_clone,
    transcribe,
    translate,
    enhance,
    stats,
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
    threading.Thread(target=_prefetch_music_model, daemon=True).start()
    threading.Thread(target=_prefetch_xtts_model, daemon=True).start()
    threading.Thread(target=_prefetch_whisper_model, daemon=True).start()
    yield


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
async def log_requests(request: Request, call_next):
    """Log all HTTP requests for audit trail."""
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
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(tts.router)
app.include_router(voices.router)
app.include_router(history.router)
app.include_router(audio.router)
app.include_router(music.router)
app.include_router(voice_clone.router)
app.include_router(transcribe.router)
app.include_router(translate.router)
app.include_router(enhance.router)
app.include_router(stats.router)

# ---------------------------------------------------------------------------
# Static files (must be mounted after all API routes)
# ---------------------------------------------------------------------------

_static_dir = BASE_DIR / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

