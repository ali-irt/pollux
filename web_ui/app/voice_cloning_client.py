"""
Drop this file into your main app.
Set VOICE_CLONING_API env var to the server.py URL (default: http://localhost:8000).

Usage:
    from voice_cloning_client import clone_voice, transcribe, transcribe_with_diarization

    # Voice cloning — returns MP3 bytes
    audio = clone_voice(
        text="Hello, welcome to our app.",
        ref_audio_path="/path/to/reference.wav",   # or pass ref_audio_bytes=...
    )
    with open("output.mp3", "wb") as f:
        f.write(audio)

    # Simple transcription
    text = transcribe(audio_path="/path/to/recording.wav")

    # Transcription + speaker diarization
    result = transcribe_with_diarization(audio_path="/path/to/meeting.wav", num_speakers=2)
    print(result["text"])        # "[SPEAKER_00] Hello\n[SPEAKER_01] Hi"
    print(result["segments"])    # list of {speaker, start, end, text}
"""

import base64
import os
from pathlib import Path
from typing import Optional

import requests

_BASE = os.environ.get("VOICE_CLONING_API", "http://localhost:8008").rstrip("/")
_TIMEOUT_GENERATE = 300   # seconds — TTS on CPU can be slow
_TIMEOUT_ASR = 120


def _encode_file(path: str) -> tuple[str, str]:
    """Return (base64_string, format_extension) for an audio file."""
    p = Path(path)
    b64 = base64.b64encode(p.read_bytes()).decode()
    fmt = p.suffix.lstrip(".").lower() or "wav"
    return b64, fmt


def clone_voice(
    text: str,
    ref_audio_path: Optional[str] = None,
    ref_audio_bytes: Optional[bytes] = None,
    ref_audio_format: str = "wav",
) -> bytes:
    """
    Generate speech in the cloned voice. Returns MP3 bytes.

    Args:
        text:             Text to synthesise.
        ref_audio_path:   Path to a WAV/MP3 reference clip (used for cloning).
        ref_audio_bytes:  Raw audio bytes (alternative to ref_audio_path).
        ref_audio_format: Format of ref_audio_bytes ('wav', 'mp3', etc.).

    Returns:
        MP3 audio as bytes. Write to a file or stream directly to the client.
    """
    payload: dict = {"target_text": text}

    if ref_audio_path:
        b64, fmt = _encode_file(ref_audio_path)
        payload["ref_audio_wav_base64"] = b64
        payload["ref_audio_wav_format"] = fmt
    elif ref_audio_bytes:
        payload["ref_audio_wav_base64"] = base64.b64encode(ref_audio_bytes).decode()
        payload["ref_audio_wav_format"] = ref_audio_format

    resp = requests.post(f"{_BASE}/generate", json=payload, timeout=_TIMEOUT_GENERATE)
    resp.raise_for_status()
    return resp.content   # MP3 bytes


def transcribe(
    audio_path: Optional[str] = None,
    audio_bytes: Optional[bytes] = None,
    audio_format: str = "wav",
) -> str:
    """
    Transcribe audio to text (no speaker labels).

    Returns plain transcript string.
    """
    if audio_path:
        b64, fmt = _encode_file(audio_path)
    else:
        b64 = base64.b64encode(audio_bytes).decode()
        fmt = audio_format

    resp = requests.post(
        f"{_BASE}/asr",
        json={"wav_base64": b64, "wav_format": fmt, "diarize": False},
        timeout=_TIMEOUT_ASR,
    )
    resp.raise_for_status()
    return resp.json().get("text", "")


def transcribe_with_diarization(
    audio_path: Optional[str] = None,
    audio_bytes: Optional[bytes] = None,
    audio_format: str = "wav",
    num_speakers: Optional[int] = None,
) -> dict:
    """
    Transcribe audio and label each segment with a speaker ID.
    Requires HF_TOKEN set in server.py's environment.

    Returns:
        {
            "text": "[SPEAKER_00] Hello\n[SPEAKER_01] Hi there",
            "segments": [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.8, "text": "Hello"},
                {"speaker": "SPEAKER_01", "start": 2.1, "end": 3.5, "text": "Hi there"},
            ]
        }
    """
    if audio_path:
        b64, fmt = _encode_file(audio_path)
    else:
        b64 = base64.b64encode(audio_bytes).decode()
        fmt = audio_format

    payload = {
        "wav_base64": b64,
        "wav_format": fmt,
        "diarize": True,
    }
    if num_speakers:
        payload["num_speakers"] = num_speakers

    resp = requests.post(f"{_BASE}/asr", json=payload, timeout=_TIMEOUT_ASR)
    resp.raise_for_status()
    return resp.json()


def is_available() -> bool:
    """Check if the voice cloning server is reachable."""
    try:
        resp = requests.get(f"{_BASE}/info", timeout=5)
        return resp.ok
    except requests.RequestException:
        return False
