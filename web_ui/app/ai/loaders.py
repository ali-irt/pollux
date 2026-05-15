"""
app/ai/loaders.py
Lazy model loaders with locks and global caches.
Includes HuggingFace cache-check helper and startup prefetch functions.
"""
import logging
import threading

from fastapi import HTTPException

from app.config import logger as _root_logger, ACE_STEP_LOCAL_DIR  # noqa: F401

logger = logging.getLogger(__name__)


def _patch_torchaudio_codec_fallback() -> None:
    """Replace torchaudio.load/save with soundfile fallbacks if torchcodec is unavailable.

    torchaudio ≥ 2.9 routes load/save through torchcodec, which requires
    FFmpeg shared DLLs at runtime.  When those DLLs are missing (e.g. only a
    static FFmpeg build is installed) every torchaudio.load() call raises a
    RuntimeError before reading a single byte.  soundfile handles WAV/FLAC/OGG
    natively without FFmpeg, so it is a safe drop-in for our use-case.
    """
    try:
        # Quick smoke-test: if the shared libraries load cleanly we don't need the patch.
        from torchcodec._core.ops import load_torchcodec_shared_libraries  # noqa: F401
        load_torchcodec_shared_libraries()
        return
    except Exception:
        pass

    try:
        import numpy as _np
        import soundfile as _sf
        import torch as _torch
        import torchaudio as _torchaudio

        def _sf_load(uri, frame_offset=0, num_frames=-1, normalize=True,
                     channels_first=True, **_kw):
            if hasattr(uri, "read"):
                data, sr = _sf.read(uri, dtype="float32", always_2d=True)
            else:
                data, sr = _sf.read(str(uri), dtype="float32", always_2d=True)
            # data: [time, channels]
            if frame_offset > 0:
                data = data[frame_offset:]
            if num_frames > 0:
                data = data[:num_frames]
            tensor = _torch.from_numpy(data.T.copy() if channels_first else data.copy())
            return tensor, sr

        def _sf_save(uri, src, sample_rate, channels_first=True, **_kw):
            if not isinstance(src, _np.ndarray):
                src = src.numpy()
            data = src.T if (src.ndim == 2 and channels_first) else src
            _sf.write(str(uri), data, sample_rate)

        _torchaudio.load = _sf_load
        _torchaudio.save = _sf_save
        logger.warning(
            "torchcodec shared libraries unavailable (FFmpeg shared build missing); "
            "patched torchaudio.load/save with soundfile fallbacks."
        )
    except Exception as exc:
        logger.warning("torchaudio codec fallback patch failed: %s", exc)


def _is_hf_cached(repo_id: str) -> bool:
    """Return True if the HuggingFace repo is already in the local disk cache."""
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo_id, local_files_only=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ACE-Step 1.5
# ---------------------------------------------------------------------------

_ace_step_pipeline = None
_ace_step_lock = threading.Lock()

_ACE_STEP_MODEL_ID = "stepfun-ai/ACE-Step-v1.5-3.5B"


def _load_ace_step():
    """Load ACE-Step 1.5 once and keep it in memory."""
    global _ace_step_pipeline
    if _ace_step_pipeline is not None:
        return _ace_step_pipeline
    with _ace_step_lock:
        if _ace_step_pipeline is not None:
            return _ace_step_pipeline
        try:
            import torch

            # torch.xpu (Intel GPU) was added in PyTorch 2.4; shim it before
            # importing ACEStepPipeline so module-level device detection doesn't
            # crash on older builds.
            if not hasattr(torch, "xpu"):
                class _StubXpu:
                    def is_available(self): return False
                    def __getattr__(self, name): return lambda *a, **kw: None
                torch.xpu = _StubXpu()

            # torch.distributed.device_mesh is a submodule added in PyTorch 2.1
            # but not auto-imported; diffusers checks for it via hasattr and
            # crashes if it hasn't been imported yet.
            try:
                import torch.distributed.device_mesh  # noqa: F401
            except Exception:
                pass

            _patch_torchaudio_codec_fallback()

            from acestep.pipeline_ace_step import ACEStepPipeline

            if torch.cuda.is_available():
                device = "cuda"
                dtype = "bfloat16"
                vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
                cpu_offload = vram_gb < 20
                quantized = vram_gb < 6
            else:
                device = "cpu"
                dtype = "float32"
                cpu_offload = False
                quantized = False

            _ace_step_pipeline = ACEStepPipeline(
                checkpoint_dir=str(ACE_STEP_LOCAL_DIR),
                dtype=dtype,
                device=device,
                cpu_offload=cpu_offload,
                quantized=quantized,
            )

            # On CPU, eagerly load the checkpoint now so we can apply dynamic
            # int8 quantization before any inference request arrives.
            # quantize_dynamic replaces nn.Linear with int8 kernels that are
            # ~2x faster on CPU with negligible quality loss.
            if device == "cpu" and not quantized:
                _ace_step_pipeline.load_checkpoint(_ace_step_pipeline.checkpoint_dir)
                try:
                    import torch.quantization as _tq
                    _ace_step_pipeline.ace_step_transformer = _tq.quantize_dynamic(
                        _ace_step_pipeline.ace_step_transformer,
                        {torch.nn.Linear},
                        dtype=torch.qint8,
                    )
                    _ace_step_pipeline.text_encoder_model = _tq.quantize_dynamic(
                        _ace_step_pipeline.text_encoder_model,
                        {torch.nn.Linear},
                        dtype=torch.qint8,
                    )
                    logger.info("ACE-Step: dynamic int8 quantization applied (CPU mode)")
                except Exception as _qe:
                    logger.warning("ACE-Step: dynamic quantization skipped: %s", _qe)

            logger.info(
                "ACE-Step 1.5 loaded on %s (dtype=%s, cpu_offload=%s, quantized=%s)",
                device, dtype, cpu_offload, quantized,
            )
            return _ace_step_pipeline
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"ace-step not installed: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load ACE-Step 1.5: {e}")


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


_ACE_STEP_REQUIRED_SUBDIRS = ["music_dcae_f8c8", "music_vocoder", "ace_step_transformer", "umt5-base"]


def _prefetch_ace_step_model():
    try:
        already_downloaded = all(
            (ACE_STEP_LOCAL_DIR / d).exists() for d in _ACE_STEP_REQUIRED_SUBDIRS
        )
        if not already_downloaded:
            logger.info("ACE-Step 1.5 not found locally — downloading (no symlinks)…")
            from huggingface_hub import snapshot_download
            ACE_STEP_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id=_ACE_STEP_MODEL_ID,
                local_dir=str(ACE_STEP_LOCAL_DIR),
                local_dir_use_symlinks=False,
            )
            logger.info("ACE-Step 1.5 download complete")
        _load_ace_step()
        logger.info("ACE-Step 1.5 loaded and ready")
    except Exception as e:
        logger.warning(f"ACE-Step startup load failed (will retry on first request): {e}")


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
