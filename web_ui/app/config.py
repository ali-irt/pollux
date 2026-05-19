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
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

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
# ---------------------------------------------------------------------------
# Job timeout limits (seconds)
# ---------------------------------------------------------------------------


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
# Song generation constants (ACE-Step 1.5)
# ---------------------------------------------------------------------------

MAX_SONG_LYRICS_LENGTH = 5000
ACE_STEP_MODEL_ID = "ACE-Step/Ace-Step1.5"
ACE_STEP_LOCAL_DIR = MODELS_DIR / "ace-step-1.5"
ACE_STEP_MAX_DURATION = 240  # seconds

# Comma-separated tag strings used as the ACE-Step `prompt` parameter.
# More specific tags = better model adherence (Suno-style descriptors).
ACE_STEP_STYLE_TAGS = {
    "pop":        "pop, catchy melody, polished studio production, vocal harmonies, radio-ready, upbeat",
    "ballad":     "ballad, slow tempo, emotional piano, heartfelt vocals, intimate, crescendo",
    "hiphop":     "hip hop, trap, 808 bass, hi-hats, rhythmic rap flow, hard-hitting beat",
    "rock":       "rock, electric guitar riffs, crashing drums, powerful lead vocals, distortion, anthem",
    "jazz":       "jazz, smooth saxophone, walking bass, piano improvisation, swing rhythm, lounge",
    "rnb":        "r&b, soul, groovy bassline, silky smooth vocals, lush chords, sensual",
    "electronic": "electronic, synth leads, four-on-the-floor EDM, pulsing bass, atmospheric pads",
    "acoustic":   "acoustic folk, fingerpicked guitar, warm vocals, intimate, unplugged, coffeehouse",
    "classical":  "orchestral, strings, cinematic swell, grand piano, classical composition, epic",
    "country":    "country, twangy guitar, fiddle, storytelling vocals, heartfelt, Southern charm",
    "reggae":     "reggae, island vibe, offbeat skank guitar, deep bass, relaxed, Rastafari",
    "metal":      "heavy metal, down-tuned guitar riffs, blast beats, aggressive screaming vocals, brutal",
    "lofi":       "lo-fi hip hop, chill beats, vinyl crackle, mellow chords, relaxing, study music",
    "latin":      "latin pop, salsa, percussion, trumpet, infectious groove, passionate vocals",
    "qawwali":    "qawwali, sufi, harmonium, tabla, Pakistani devotional, call and response vocals, urdu",
    "ghazal":     "ghazal, urdu poetry, classical vocals, intimate, sitar, tabla, melancholic, Pakistani",
    "pakistani_pop": "Pakistani pop, urdu vocals, melodic, dholak, contemporary South Asian production",
    "bollywood":  "bollywood, Indian film music, orchestral, emotional hindi vocals, dramatic, lush strings",
    "punjabi":    "punjabi, bhangra, dhol drums, energetic, folk vocals, desi beat, festive",
}

# Inference quality presets: (infer_step, scheduler_type, guidance_scale)
# guidance_scale must stay >= 7.0 — lower values let noise dominate vocals.
# Speed comes from fewer steps, not lower guidance.
ACE_STEP_QUALITY_PRESETS = {
    "turbo":    (10,  "euler", 7.0),   # ~2–4 min on CPU; audible vocals, some noise
    "fast":     (20,  "euler", 7.0),   # ~4–8 min on CPU; clean vocals
    "balanced": (30,  "euler", 7.0),   # ~8–15 min on CPU; solid quality
    "best":     (60,  "heun",  7.5),   # ~15–30 min on CPU; maximum quality
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
# VoxCPM voice cloning microservice
# ---------------------------------------------------------------------------

VOXCPM_API_URL = os.environ.get("VOICE_CLONING_API", "http://localhost:8008")
VOXCPM_MAX_AUDIO_BYTES = 25 * 1024 * 1024   # 25 MB
VOXCPM_ALLOWED_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PREMIUM_PRICE_CENTS = 500  # $5.00

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://pollux:pollux@localhost:5432/pollux",
)
