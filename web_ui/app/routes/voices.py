"""
app/routes/voices.py
Voice listing endpoints.
"""
import logging
import urllib.request

from fastapi import APIRouter, Depends

from app.config import MODELS_DIR, PREMIUM_QUALITIES
from app.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_voices_json() -> dict:
    voices_json_path = MODELS_DIR / "voices.json"
    if not voices_json_path.exists():
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/voices.json"
        urllib.request.urlretrieve(url, voices_json_path)
    import json
    with open(voices_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def annotate_voices(voices: dict, is_premium_user: bool = True) -> dict:
    for info in voices.values():
        info["is_premium"] = False
        info["locked"] = False
    return voices


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/voices", summary="All voices with premium flags")
def get_voices(user=Depends(get_current_user)):
    voices = annotate_voices(load_voices_json(), user["is_premium"])
    return list(voices.values())
