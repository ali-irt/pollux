# VoiceWave API — Mobile Developer Reference

Base URL: configured in `BASE_URL` env var (e.g. `http://api.dynamicare.live`)  
Interactive docs: `{BASE_URL}/docs`  
OpenAPI JSON: `{BASE_URL}/openapi.json`

---

## Authentication

All protected endpoints require:
```
Authorization: Bearer <access_token>
```

Tokens are JWTs. Access tokens expire in 1 hour. Use the refresh token to get a new one without re-login.

---

## Error Format

All errors return:
```json
{ "detail": "Human-readable error message" }
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request / validation error |
| 401 | Missing or invalid token |
| 404 | Resource not found |
| 409 | Conflict (e.g. email already registered) |
| 422 | Request body schema error |
| 429 | Rate limit exceeded |
| 500 | Server error |

---

## Auth

### POST `/api/auth/register`
No auth required. Rate limit: 5/min.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "StrongPass123!"
}
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "user": {
    "email": "user@example.com",
    "plan": "free",
    "generation_count": 0,
    "credits": 50,
    "is_premium": false,
    "generations_remaining": 50
  }
}
```

---

### POST `/api/auth/login`
No auth required. Rate limit: 10/min.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "StrongPass123!"
}
```

**Response:** same shape as `/register`.

---

### POST `/api/auth/refresh`
No auth required. Call this when the access token expires.

**Request:**
```json
{ "refresh_token": "eyJ..." }
```

**Response:**
```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

---

### GET `/api/auth/me`
Auth required.

**Response:**
```json
{
  "id": 1,
  "email": "user@example.com",
  "plan": "free",
  "generation_count": 12,
  "credits": 38,
  "is_premium": false,
  "generations_remaining": 38,
  "member_since": "2026-05-01T10:00:00"
}
```

---

### POST `/api/auth/change-password`
Auth required.

**Request:**
```json
{
  "current_password": "OldPass123!",
  "new_password": "NewPass456!"
}
```

**Response:**
```json
{ "success": true, "message": "Password updated successfully." }
```

---

### POST `/api/auth/upgrade-to-premium`
Auth required. Grants unlimited credits (`credits: -1`).

**Response:**
```json
{ "success": true, "plan": "premium", "is_premium": true, "credits": -1 }
```

---

## Text-to-Speech (TTS)

### GET `/api/voices`
Auth required. Returns all available voices.

**Response:** Array of voice objects:
```json
[
  {
    "name": "ur-PK-UzmaNeural",
    "language": "ur",
    "engine": "edge-tts",
    "is_premium": false,
    "locked": false
  }
]
```

---

### POST `/api/generate`
Auth required. Rate limit: 20/min.  
Returns audio bytes directly (not a job — synchronous).

**Request:**
```json
{
  "text": "Hello world",
  "model": "ur-PK-UzmaNeural",
  "speed": 1.0,
  "pitch_hz": 0
}
```

| Field | Type | Range | Notes |
|-------|------|-------|-------|
| `text` | string | required | |
| `model` | string | required | Voice ID from `/api/voices` |
| `speed` | float | 0.5 – 2.0 | Default `1.0` |
| `pitch_hz` | int | -10 – 10 | Edge-TTS only. Default `0` |

**Response:** Audio file bytes.  
`Content-Type: audio/mpeg` (edge-tts) or `audio/wav` (piper)

---

## AI Song Generation

Song generation is **async** — submit a job, poll until done, then download.

### POST `/api/generate_music`
Auth required.

**Request:**
```json
{
  "lyrics": "تیری یاد آئی ہے\nدل میں درد ہے",
  "style": "ghazal",
  "language": "urdu",
  "audio_duration": 60,
  "quality": "fast"
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `lyrics` | string | required | Max 5000 chars. Supports any language/script |
| `style` | string | `"pop"` | See styles below |
| `language` | string | `"english"` | Steers vocal accent |
| `audio_duration` | int | `30` | Seconds, 10–240 |
| `quality` | string | `"fast"` | See presets below |

**Styles:**
`pop`, `ballad`, `hiphop`, `rock`, `jazz`, `rnb`, `electronic`, `acoustic`, `classical`, `country`, `reggae`, `metal`, `lofi`, `latin`, `qawwali`, `ghazal`, `pakistani_pop`, `bollywood`, `punjabi`

**Quality presets:**

| Preset | Steps | Time (GPU) | Notes |
|--------|-------|------------|-------|
| `turbo` | 10 | ~1 min | Fast, audible quality |
| `fast` | 20 | ~2 min | Good quality |
| `balanced` | 30 | ~3–5 min | Recommended |
| `best` | 60 | ~10 min | Maximum quality |

**Languages with accent support:**
`english`, `urdu`, `hindi`, `punjabi`, `arabic`, `spanish`, `french`, `korean`, `japanese`

**Response:**
```json
{
  "job_id": "abc123",
  "status": "pending",
  "poll_url": "/api/jobs/abc123"
}
```

---

### GET `/api/jobs/{job_id}`
Auth required. Poll this every 3–5 seconds until `status` is `done` or `failed`.

**Response (pending/processing):**
```json
{
  "id": "abc123",
  "status": "processing",
  "type": "song",
  "created_at": "2026-05-16T12:00:00",
  "updated_at": "2026-05-16T12:00:05"
}
```

**Response (done):**
```json
{
  "id": "abc123",
  "status": "done",
  "type": "song",
  "download_url": "/api/jobs/abc123/download",
  "created_at": "2026-05-16T12:00:00",
  "updated_at": "2026-05-16T12:03:00"
}
```

**Response (failed):**
```json
{
  "id": "abc123",
  "status": "failed",
  "error_message": "Remote ACE-Step job failed: ..."
}
```

---

### GET `/api/jobs/{job_id}/download`
Auth required. Downloads the MP3 file. **One-time** — file is deleted after download.

**Response:** MP3 audio bytes.  
`Content-Type: audio/mpeg`  
`Content-Disposition: attachment; filename=abc123.mp3`

---

### GET `/api/music/options`
Auth required. Returns all valid values for style and quality.

```json
{
  "model": "ace-step-1.5",
  "max_duration": 240,
  "styles": [
    { "id": "pop", "label": "Pop", "tags": "pop, catchy melody..." }
  ],
  "quality_presets": [
    { "id": "fast", "label": "Fast", "infer_steps": 20, "scheduler": "euler", "guidance_scale": 7.0 }
  ]
}
```

---

## Voice Cloning (XTTS v2)

Voice cloning is also **async** — same poll pattern as song generation.

### POST `/api/voice_clone/upload`
Auth required. Rate limit: 10/min. Multipart form.

| Field | Type | Notes |
|-------|------|-------|
| `file` | audio file | WAV/MP3/OGG/FLAC/M4A, max 10 MB, 3–30 sec |
| `name` | string | Label for this voice |

**Response:**
```json
{ "job_id": "xyz789", "status": "pending", "poll_url": "/api/jobs/xyz789" }
```

---

### GET `/api/voice_clone/list`
Auth required. Lists saved voice samples.

---

### DELETE `/api/voice_clone/{voice_id}`
Auth required.

---

### POST `/api/voice_clone/generate`
Auth required. Generate speech using a saved cloned voice.

**Request:**
```json
{
  "voice_id": 3,
  "text": "Hello, this is my cloned voice.",
  "language": "en"
}
```

**Response:** `{ "job_id": "...", "status": "pending" }` — poll and download same as song.

---

### POST `/api/voice_clone/generate_oneshot`
Auth required. One-shot: upload reference audio + synthesise in one call. Multipart form.

| Field | Type | Notes |
|-------|------|-------|
| `file` | audio file | Reference audio (3–30 sec) |
| `text` | string | Text to synthesise |
| `language` | string | e.g. `en`, `ur`, `hi` |

**Response:** `{ "job_id": "...", "status": "pending" }`

---

### GET `/api/voice_clone/languages`
Auth required. Returns supported languages.

---

## Speech-to-Text (Whisper)

### POST `/api/transcribe`
Auth required. Multipart form.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `file` | audio file | required | WAV/MP3/OGG/FLAC/M4A/WEBM/MP4, max 50 MB |
| `model_size` | string | `base` | `tiny` / `base` / `small` |
| `language` | string | `auto` | e.g. `en`, `ur`, `hi` or `auto` |

**Response:**
```json
{
  "success": true,
  "text": "Full transcribed text...",
  "segments": [
    { "start": 0.0, "end": 2.5, "text": "Hello world" }
  ],
  "model": "openai/whisper-base",
  "language_hint": "auto",
  "word_count": 2,
  "character_count": 11,
  "segment_count": 1
}
```

---

## Translation

### POST `/api/translate`
Auth required.

**Request:**
```json
{
  "text": "Hello world",
  "target_lang": "urdu"
}
```

**Response:**
```json
{ "success": true, "translated_text": "ہیلو دنیا" }
```

---

### GET `/api/languages`
No auth required. Returns all supported translation language codes.

---

## Audio Enhancement

### POST `/api/enhance_audio`
Auth required. Multipart form. Returns enhanced audio bytes synchronously.

| Field | Type | Default | Range | Notes |
|-------|------|---------|-------|-------|
| `file` | audio file | required | | WAV/MP3/OGG/FLAC, max 50 MB |
| `normalize` | bool | `true` | | Normalize loudness |
| `fade_in` | float | `0.0` | 0–5 sec | |
| `fade_out` | float | `0.0` | 0–5 sec | |
| `reverb_amount` | float | `0.0` | 0.0–1.0 | |
| `pitch_steps` | int | `0` | -6 – 6 | Semitones |
| `speed_factor` | float | `1.0` | 0.5–2.0 | |

**Response:** WAV audio bytes. `Content-Type: audio/wav`

---

## History

### GET `/api/history?page=1&per_page=20`
Auth required.

**Response:**
```json
{
  "page": 1,
  "per_page": 20,
  "total": 42,
  "total_pages": 3,
  "items": [
    {
      "id": 10,
      "model": "ace-step-1.5:ghazal",
      "text_snippet": "تیری یاد آئی ہے...",
      "created_at": "2026-05-16T12:03:00"
    }
  ]
}
```

---

### DELETE `/api/history/{entry_id}`
Auth required.

```json
{ "success": true, "deleted_id": 10 }
```

---

### DELETE `/api/history`
Auth required. Clears all history.

```json
{ "success": true, "deleted_count": 42 }
```

---

## Stats

### GET `/api/stats`
Auth required.

```json
{
  "plan": "free",
  "generation_count": 12,
  "generations_remaining": 38,
  "credits": 38,
  "is_premium": false,
  "total_in_history": 12,
  "last_generation": "2026-05-16T12:03:00",
  "top_voices": [
    { "model": "ace-step-1.5:ghazal", "count": 5 }
  ],
  "member_since": "2026-05-01T10:00:00"
}
```

---

## Info

### GET `/api/models`
No auth required. Returns all AI models with display metadata.

### GET `/api/endpoints`
No auth required. Returns structured list of every endpoint with URLs — useful for auto-configuring your API client.

### GET `/health`
No auth required. DB health check used by load balancers.

---

## Typical Mobile Flow — Song Generation

```
1. POST /api/auth/login          → access_token, refresh_token
2. POST /api/generate_music      → job_id
3. GET  /api/jobs/{job_id}       → poll every 3–5s until status = "done"
4. GET  /api/jobs/{job_id}/download → MP3 bytes → save to device
```

## Token Refresh Flow

```
Access token expires (401 response)
  → POST /api/auth/refresh  { refresh_token }
  → new access_token
  → retry original request
```
