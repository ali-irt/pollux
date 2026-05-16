"""
VoiceWave ACE-Step ZeroGPU Server
Deployed on HuggingFace Spaces — provides the same REST API that
_call_remote_ace_step() in workers.py expects:
  POST /generate  → { job_id }
  GET  /result/{job_id} → JSON status or WAV bytes
"""
import os
import uuid
import threading
import traceback
import inspect
from pathlib import Path

import spaces                          # HF ZeroGPU
import gradio as gr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Model setup — loaded once at startup
# ---------------------------------------------------------------------------

MODEL_ID  = "ACE-Step/Ace-Step1.5"
LOCAL_DIR = "/tmp/ace_step_model"
RESULTS_DIR = Path("/tmp/ace_step_results")
RESULTS_DIR.mkdir(exist_ok=True)

pipeline = None
_pipeline_lock = threading.Lock()


def _load_pipeline():
    global pipeline
    if pipeline is not None:
        return pipeline
    with _pipeline_lock:
        if pipeline is not None:
            return pipeline

        import torch
        from huggingface_hub import snapshot_download
        from acestep.pipeline_ace_step import ACEStepPipeline

        # Disable Flash Attention 2 — ZeroGPU may not always get Ampere
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_math_sdp(True)

        os.makedirs(LOCAL_DIR, exist_ok=True)
        snapshot_download(repo_id=MODEL_ID, local_dir=LOCAL_DIR,
                          local_dir_use_symlinks=False)

        vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9 if torch.cuda.is_available() else 0
        sm      = torch.cuda.get_device_capability(0) if torch.cuda.is_available() else (0, 0)
        device  = "cuda" if torch.cuda.is_available() else "cpu"
        dtype   = "bfloat16" if sm[0] >= 8 else "float16"

        pipeline = ACEStepPipeline(
            checkpoint_dir=LOCAL_DIR,
            dtype=dtype,
            device=device,
            cpu_offload=vram_gb < 20,
            quantized=vram_gb < 6,
        )
        print(f"Pipeline loaded on {device} ({dtype})")
        return pipeline


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}   # job_id → {status, wav_path, error}


# ---------------------------------------------------------------------------
# ZeroGPU inference function
# duration=360 → requests up to 6 min of GPU time per call
# ---------------------------------------------------------------------------

@spaces.GPU(duration=360)
def _infer(out_path: str, audio_duration: int, prompt: str, lyrics: str,
           infer_step: int, guidance_scale: float, scheduler_type: str,
           guidance_interval: float, use_erg_tag: bool,
           use_erg_lyric: bool, use_erg_diffusion: bool):
    import torch
    pipe = _load_pipeline()
    sig    = inspect.signature(pipe.__call__)
    params = sig.parameters

    kwargs: dict = dict(
        audio_duration=audio_duration,
        prompt=prompt,
        lyrics=lyrics,
        infer_step=infer_step,
        guidance_scale=guidance_scale,
        scheduler_type=scheduler_type,
        save_path=out_path,
    )
    if "guidance_interval" in params:
        kwargs["guidance_interval"] = guidance_interval
    if "use_erg_tag" in params:
        kwargs["use_erg_tag"]        = use_erg_tag
        kwargs["use_erg_lyric"]      = use_erg_lyric
        kwargs["use_erg_diffusion"]  = use_erg_diffusion

    with torch.inference_mode():
        pipe(**kwargs)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_generation(job_id: str, req_data: dict):
    out_path = str(RESULTS_DIR / f"{job_id}.wav")
    try:
        _jobs[job_id]["status"] = "processing"
        _infer(
            out_path          = out_path,
            audio_duration    = req_data["audio_duration"],
            prompt            = req_data["prompt"],
            lyrics            = req_data["lyrics"],
            infer_step        = req_data["infer_step"],
            guidance_scale    = req_data["guidance_scale"],
            scheduler_type    = req_data["scheduler_type"],
            guidance_interval = req_data["guidance_interval"],
            use_erg_tag       = req_data["use_erg_tag"],
            use_erg_lyric     = req_data["use_erg_lyric"],
            use_erg_diffusion = req_data["use_erg_diffusion"],
        )
        _jobs[job_id] = {"status": "done", "wav_path": out_path, "error": None}
        print(f"[{job_id[:8]}] done")
    except Exception as exc:
        print(f"[{job_id[:8]}] FAILED:\n{traceback.format_exc()}")
        _jobs[job_id] = {"status": "failed", "wav_path": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

api = FastAPI()


class GenerateRequest(BaseModel):
    audio_duration:    int   = 30
    prompt:            str   = ""
    lyrics:            str   = ""
    infer_step:        int   = 60
    guidance_scale:    float = 7.0
    scheduler_type:    str   = "euler"
    guidance_interval: float = 0.5
    use_erg_tag:       bool  = True
    use_erg_lyric:     bool  = False
    use_erg_diffusion: bool  = False


@api.post("/generate")
def generate(req: GenerateRequest):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending", "wav_path": None, "error": None}
    threading.Thread(
        target=_run_generation,
        args=(job_id, req.model_dump()),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@api.get("/result/{job_id}")
def result(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "done":
        return FileResponse(job["wav_path"], media_type="audio/wav",
                            filename=f"{job_id}.wav")
    if job["status"] == "failed":
        return JSONResponse({"status": "failed", "error": job["error"]})
    return JSONResponse({"status": job["status"]})


@api.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID}


# ---------------------------------------------------------------------------
# Gradio UI (minimal — required by HF Spaces SDK)
# Mounts the FastAPI app so /generate and /result/* are accessible
# ---------------------------------------------------------------------------

with gr.Blocks(title="VoiceWave ACE-Step Server") as demo:
    gr.Markdown("""
# VoiceWave ACE-Step GPU Server
REST API is live. Use these endpoints from your backend:
- `POST /generate` — submit generation job
- `GET /result/{job_id}` — poll / download result
- `GET /health` — health check
    """)

app = gr.mount_gradio_app(api, demo, path="/ui")
