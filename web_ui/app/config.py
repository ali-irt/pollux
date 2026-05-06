"""
app/config.py
Constants, environment variables, and path definitions.
This module must NOT import from any other app/ module.
"""
import os
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename='pollux.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory Configuration
# BASE_DIR is the parent of web_ui/main.py — keep it anchored there so
# all existing path references (models/, outputs/, voice_samples/) work.
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.parent   # web_ui/
OUTPUTS_DIR = BASE_DIR / "outputs"
MODELS_DIR = BASE_DIR / "models"
VOICE_SAMPLES_DIR = BASE_DIR / "voice_samples"
CLONED_VOICES_DIR = BASE_DIR / "cloned_voices"
DB_PATH = BASE_DIR / "pollux.db"

OUTPUTS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
VOICE_SAMPLES_DIR.mkdir(exist_ok=True)
CLONED_VOICES_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# JWT / Auth
# ---------------------------------------------------------------------------

JWT_SECRET = os.environ.get("POLLUX_JWT_SECRET")
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise ValueError("POLLUX_JWT_SECRET must be set and at least 32 characters")

JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Credits & plans
# ---------------------------------------------------------------------------

INITIAL_FREE_CREDITS = 50
CREDITS_PER_GENERATION = 5
PREMIUM_QUALITIES = set()  # all voices free

# ---------------------------------------------------------------------------
# Input length limits
# ---------------------------------------------------------------------------

MAX_EMAIL_LENGTH = 254           # RFC 5321
MAX_PASSWORD_LENGTH = 56
MIN_PASSWORD_LENGTH = 8
MAX_TEXT_LENGTH = 5000
MAX_TRANSLATION_LENGTH = 5000
MAX_MUSIC_PROMPT_LENGTH = 1000

# ---------------------------------------------------------------------------
# Job timeout limits (seconds)
# ---------------------------------------------------------------------------


JOB_TIMEOUT_MUSIC = 300
JOB_TIMEOUT_SONG = 360
JOB_TIMEOUT_VOICE_CLONE = 550


# ---------------------------------------------------------------------------
# CORS
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
# Audio / enhance constants
# ---------------------------------------------------------------------------

ENHANCE_ALLOWED_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
ENHANCE_MAX_SIZE = 50 * 1024 * 1024   # 50 MB

# ---------------------------------------------------------------------------
# Whisper constants
# ---------------------------------------------------------------------------

MAX_TRANSCRIBE_SIZE = 50 * 1024 * 1024  # 50 MB
WHISPER_ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".flac", ".webm", ".opus"}
WHISPER_ALLOWED_SIZES = {"tiny", "base", "small"}

# ---------------------------------------------------------------------------
# Song generation constants
# ---------------------------------------------------------------------------

MAX_SONG_LYRICS_LENGTH = 800

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
    "ar_singer_1": "v2/en_speaker_6",
    "tr_singer_1": "v2/tr_speaker_2",
    "ru_singer_1": "v2/ru_speaker_2",
    "pt_singer_1": "v2/pt_speaker_2",
}

SONG_VOICE_TO_EDGE = {
    "en_singer_1": "en-US-GuyNeural",
    "en_singer_2": "en-US-DavisNeural",
    "en_singer_3": "en-US-JennyNeural",
    "en_singer_4": "en-US-AriaNeural",
    "en_female_1": "en-US-SaraNeural",
    "en_female_2": "en-US-NancyNeural",
    "zh_singer_1": "zh-CN-XiaoxiaoNeural",
    "es_singer_1": "es-ES-ElviraNeural",
    "fr_singer_1": "fr-FR-DeniseNeural",
    "de_singer_1": "de-DE-KatjaNeural",
    "hi_singer_1": "hi-IN-SwaraNeural",
    "ar_singer_1": "ar-SA-ZariyahNeural",
    "tr_singer_1": "tr-TR-EmelNeural",
    "ru_singer_1": "ru-RU-SvetlanaNeural",
    "pt_singer_1": "pt-BR-FranciscaNeural",
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

# ---------------------------------------------------------------------------
# Voice cloning constants
# ---------------------------------------------------------------------------

CLONE_ALLOWED_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
CLONE_MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB
CLONE_MIN_DURATION = 3.0                  # seconds
CLONE_MAX_DURATION = 30.0                 # seconds
CLONE_MAX_PER_USER = 10                   # max saved voice profiles

MAX_REFERENCE_AUDIO_SIZE = 25 * 1024 * 1024   # 25 MB
MAX_VOICE_PROFILE_NAME_LENGTH = 50
MAX_VOICE_PROFILES_FREE = 3
MAX_VOICE_PROFILES_PREMIUM = 20
VOICE_CLONE_ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}

XTTS_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl",
    "tr", "ru", "nl", "cs", "ar", "zh-cn",
    "hu", "ko", "ja", "hi",
}

# XTTS-v2 supports these BCP-47 language codes (second definition in original)
XTTS_SUPPORTED_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl", "tr",
    "ru", "nl", "cs", "ar", "zh-cn", "hu", "ko", "ja", "hi",
}

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
