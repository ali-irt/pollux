"""
app/ai/loaders.py
Lazy model loaders with locks and global caches.
Includes HuggingFace cache-check helper and startup prefetch functions.
"""
import logging
import threading

from fastapi import HTTPException

from app.config import logger as _root_logger  # noqa: F401

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MusicGen
# ---------------------------------------------------------------------------

_musicgen_processor = None
_musicgen_model = None
_musicgen_lock = threading.Lock()


def _is_hf_cached(repo_id: str) -> bool:
    """Return True if the HuggingFace repo is already in the local disk cache."""
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo_id, local_files_only=True)
        return True
    except Exception:
        return False


def _load_musicgen():
    """Load MusicGen once and keep it in memory for all subsequent calls."""
    global _musicgen_processor, _musicgen_model
    with _musicgen_lock:
        if _musicgen_processor is not None and _musicgen_model is not None:
            return _musicgen_processor, _musicgen_model
        try:
            import torch
            from transformers import AutoProcessor, MusicgenForConditionalGeneration

            if torch.backends.mps.is_available():
                device, dtype = "mps", torch.float32
            elif torch.cuda.is_available():
                device, dtype = "cuda", torch.float16
            else:
                device, dtype = "cpu", torch.float32

            _musicgen_processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
            _musicgen_model = MusicgenForConditionalGeneration.from_pretrained(
                "facebook/musicgen-small",
                torch_dtype=dtype,
                low_cpu_mem_usage=True,
            ).to(device)
            logger.info(f"MusicGen loaded on {device}")
            return _musicgen_processor, _musicgen_model
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"MusicGen dependencies missing: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load MusicGen model: {e}")


# ---------------------------------------------------------------------------
# XTTS v2
# ---------------------------------------------------------------------------

_xtts_model = None
_xtts_lock = threading.Lock()


def _load_xtts():
    global _xtts_model
    # Fast path — no lock needed once the model is resident.
    if _xtts_model is not None:
        return _xtts_model
    with _xtts_lock:
        if _xtts_model is not None:
            return _xtts_model
        try:
            import torch
            from TTS.api import TTS as CoquiTTS

            # Coqui TTS checkpoints are pickle-based; PyTorch ≥ 2.6 changed the
            # torch.load default to weights_only=True which rejects them.
            # Patch torch.load for this call only, then restore.
            _orig_load = torch.load
            def _compat_load(*args, **kw):
                kw.setdefault("weights_only", False)
                return _orig_load(*args, **kw)
            torch.load = _compat_load
            try:
                device = "cuda" if torch.cuda.is_available() else "cpu"
                _xtts_model = CoquiTTS(
                    "tts_models/multilingual/multi-dataset/xtts_v2"
                ).to(device)
            finally:
                torch.load = _orig_load

            logger.info(f"XTTS v2 loaded on {device}")
            return _xtts_model
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"Coqui TTS not installed: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load XTTS v2: {e}")


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------

_whisper_pipe = None
_whisper_model_id: str | None = None


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


# ---------------------------------------------------------------------------
# Bark
# ---------------------------------------------------------------------------

_bark_processor = None
_bark_model = None
_bark_device = "cpu"


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


# ---------------------------------------------------------------------------
# Startup prefetch helpers (called from main.py startup event)
# ---------------------------------------------------------------------------


def _prefetch_music_model():
    try:
        if _is_hf_cached("facebook/musicgen-small"):
            logger.info("MusicGen already cached — skipping download")
            return
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id="facebook/musicgen-small")
        logger.info("MusicGen model files downloaded")
    except Exception as e:
        logger.warning(f"MusicGen pre-cache failed (will download on first use): {e}")


def _prefetch_xtts_model():
    try:
        _load_xtts()
        logger.info("XTTS v2 pre-warmed at startup")
    except Exception as e:
        logger.warning(f"XTTS pre-warm failed (will load on first use): {e}")


def _prefetch_whisper_model():
    try:
        _load_whisper("base")
        logger.info("Whisper base pre-warmed at startup")
    except Exception as e:
        logger.warning(f"Whisper pre-warm failed (will load on first use): {e}")


def _prefetch_bark_model():
    try:
        if _is_hf_cached("suno/bark-small"):
            logger.info("Bark already cached — skipping download")
            return
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id="suno/bark-small")
        logger.info("Bark model files downloaded")
    except Exception as e:
        logger.warning(f"Bark pre-cache failed (will download on first use): {e}")
