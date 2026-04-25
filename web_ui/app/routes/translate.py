"""
app/routes/translate.py
Translation and language listing endpoints.
"""
import logging

from deep_translator import GoogleTranslator
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import MAX_TRANSLATION_LENGTH
from app.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# Build supported language set at module load time
SUPPORTED_LANGUAGES = set(
    GoogleTranslator(source="auto", target="en").get_supported_languages(as_dict=True).values()
)  # type: ignore[attr-defined]


class TranslateRequest(BaseModel):
    text: str
    target_lang: str


@router.post("/api/translate", summary="Translate text to target language")
def translate_text(request: TranslateRequest, user=Depends(get_current_user)):
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    if len(request.text) > MAX_TRANSLATION_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds maximum length of {MAX_TRANSLATION_LENGTH} characters."
        )

    target_lang = request.target_lang.strip().lower()
    if not target_lang:
        raise HTTPException(status_code=400, detail="Target language is required.")
    if target_lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language code: {target_lang}"
        )

    try:
        translated = GoogleTranslator(source="auto", target=target_lang).translate(request.text.strip())
        return {"success": True, "translated_text": translated}
    except Exception as e:
        logger.error(f"Translation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Translation service error")


@router.get("/api/languages", summary="Get all supported languages for translation")
def get_languages():
    try:
        langs = GoogleTranslator().get_supported_languages(as_dict=True)
        return {"success": True, "languages": langs}
    except Exception as e:
        logger.error(f"Failed to retrieve languages: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve languages")
