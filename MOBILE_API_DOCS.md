# Pollux API — Mobile Developer Documentation

> **Base URL:** `http://<your-server>:8000`
> **Interactive Docs:** `http://<your-server>:8000/docs`
> **Framework:** FastAPI · **DB:** SQLite · **Auth:** JWT (HS256)

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Authentication](#2-authentication)
3. [Token Management](#3-token-management)
4. [Async Job System](#4-async-job-system)
5. [Text-to-Speech](#5-text-to-speech)
6. [Voices](#6-voices)
7. [Audio Storage](#7-audio-storage)
8. [Generation History](#8-generation-history)
9. [Voice Cloning](#9-voice-cloning)
10. [Voice Profiles](#10-voice-profiles)
11. [Audio Enhancement](#11-audio-enhancement)
12. [Speech-to-Text](#12-speech-to-text)
13. [Music Generation](#13-music-generation)
14. [Song Generation](#14-song-generation)
15. [Translation](#15-translation)
16. [User Stats](#16-user-stats)
17. [Error Reference](#17-error-reference)
18. [Timeouts Reference](#18-timeouts-reference)
19. [Constants Reference](#19-constants-reference)
20. [Response Times](#20-response-times)

---

## 1. Quick Start

### Minimal flow to generate and play audio

```
1. POST /api/auth/login            → access_token, refresh_token
2. GET  /api/voices/free           → pick a voice model name
3. POST /api/generate              → audio bytes + X-Generation-Id header
4. Play audio bytes in-app
5. GET  /api/audio/{id}            → re-play from server
6. GET  /api/audio/{id}/download   → save to device (DELETES from server)
```

### Auth header (required on all protected endpoints)

```
Authorization: Bearer <access_token>
```

---

## 2. Authentication

---

### POST `/api/auth/register`

Create a new account. Returns tokens immediately — no email verification.

**Auth:** None

**Request:**
```json
{
  "email": "user@example.com",
  "password": "StrongPass1!"
}
```

**Password rules:** 8–56 chars · uppercase · lowercase · digit · special char (`!@#$%^&*...`)

**Response `200`:**
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

**Errors:** `400` validation failed · `409` email already registered

---

### POST `/api/auth/login`

**Auth:** None | **Rate limit:** 10 req/min

**Request:**
```json
{
  "email": "user@example.com",
  "password": "StrongPass1!"
}
```

**Response `200`:** Same shape as register.

> After **5 failed attempts in 15 minutes** the account is temporarily locked.

**Errors:** `401` wrong credentials · `429` rate limited

---

### POST `/api/auth/refresh`

Swap a refresh token for a new access token. Call this when a `401` is returned on any protected endpoint.

**Auth:** None

**Request:**
```json
{ "refresh_token": "eyJ..." }
```

**Response `200`:**
```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

**Errors:** `401` invalid or expired refresh token → user must log in again

---

### GET `/api/auth/me`

Get the authenticated user's profile.

**Auth:** Required

**Response `200`:**
```json
{
  "id": 1,
  "email": "user@example.com",
  "plan": "free",
  "generation_count": 12,
  "credits": 10,
  "is_premium": false,
  "generations_remaining": 10,
  "member_since": "2025-01-01T00:00:00"
}
```

---

### POST `/api/auth/change-password`

**Auth:** Required

**Request:**
```json
{
  "current_password": "OldPass1!",
  "new_password": "NewPass2@"
}
```

**Response `200`:** `{ "success": true, "message": "Password updated successfully." }`

**Errors:** `400` weak new password · `401` wrong current password

---

### POST `/api/auth/upgrade-to-premium`

Upgrade to premium. Sets credits to `-1` (unlimited).

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "plan": "premium",
  "is_premium": true,
  "credits": -1
}
```

---

## 3. Token Management

Store both tokens securely (Keychain / EncryptedSharedPreferences).

| Token | Lifetime | Use |
|---|---|---|
| `access_token` | 24 hours | Every API call |
| `refresh_token` | 90 days | Renew access token |

### Recommended refresh strategy

```
On any 401 response:
  → POST /api/auth/refresh with refresh_token
  → If 200: store new access_token, retry original request
  → If 401: refresh_token expired → redirect to Login screen
```

---

## 4. Async Job System

Heavy AI operations (music, song, voice clone) **return a `job_id` immediately** and process in the background. The HTTP response always comes back in under 1 second.

### How it works

```
POST /api/generate_music   →  { job_id: "abc123", status: "pending" }
                                        ↓ background thread starts
poll GET /api/jobs/abc123  →  { status: "processing" }
poll GET /api/jobs/abc123  →  { status: "processing" }
poll GET /api/jobs/abc123  →  { status: "done", generation_id: 42,
                                audio_url: "/api/audio/42",
                                download_url: "/api/audio/42/download" }
```

### Job status values

| Status | Meaning |
|---|---|
| `pending` | Queued, not started yet |
| `processing` | AI model is running |
| `done` | Finished — `generation_id`, `audio_url`, `download_url` are set |
| `failed` | Error occurred — `error_message` explains why (including timeouts) |

### GET `/api/jobs/{job_id}`

**Auth:** Required

**Response `200`:**
```json
{
  "id": "a1b2c3d4e5f6g7h8",
  "user_id": 1,
  "type": "music",
  "status": "done",
  "generation_id": 42,
  "error_message": null,
  "audio_url": "/api/audio/42",
  "download_url": "/api/audio/42/download",
  "created_at": "2025-01-15T10:00:00",
  "updated_at": "2025-01-15T10:00:35"
}
```

**Errors:** `404` job not found or belongs to another user

### Polling recommendation

```
interval  = 3 seconds
max_polls = 40   (2 minutes total before giving up client-side)

while polls < max_polls:
    wait(interval)
    job = GET /api/jobs/{job_id}

    if job.status == "done":
        use job.audio_url / job.download_url
        break
    if job.status == "failed":
        show job.error_message
        break

if polls >= max_polls:
    show "Still processing — check back later"
```

### Job timeout limits (server-side)

If the AI model takes too long, the server automatically fails the job:

| Job type | Server timeout |
|---|---|
| Music generation | 60 seconds |
| Song generation | 90 seconds |
| Voice clone | 50 seconds |

On timeout, `status` becomes `"failed"` and `error_message` is `"Generation timed out after Xs"`.

---

## 5. Text-to-Speech

### POST `/api/generate`

Generate speech from text. **Synchronous** — returns audio bytes directly (no job polling needed).

**Auth:** Required | **Rate limit:** 20 req/min

**Request:**
```json
{
  "text": "Hello, world!",
  "model": "en-US-AriaNeural",
  "speed": 1.0,
  "pitch_hz": 0
}
```

| Field | Type | Range | Default |
|---|---|---|---|
| `text` | string | max 5000 chars | required |
| `model` | string | voice ID from `/api/voices` | required |
| `speed` | float | 0.5 – 2.0 | `1.0` |
| `pitch_hz` | int | -10 – +10 | `0` |

**Response `200`:** Binary audio bytes

| Header | Value | Notes |
|---|---|---|
| `Content-Type` | `audio/mpeg` or `audio/wav` | mp3 = edge-tts, wav = piper |
| `X-Generation-Id` | e.g. `"42"` | Save this — use it to stream/download later |

> **Important:** Read and store the `X-Generation-Id` response header. This is the only way to reference this audio later.

**Errors:** `400` empty text / unknown model · `429` rate limit · `500` synthesis failed / timed out (30s subprocess limit)

---

## 6. Voices

### GET `/api/voices`
All available voices with premium flags.

### GET `/api/voices/free`
Free-tier voices only (recommended starting point).

### GET `/api/voices/premium`
Premium voices only.

**Auth:** Required on all three.

**Voice object:**
```json
{
  "name": "en-US-AriaNeural",
  "language": "en-US",
  "engine": "edge-tts",
  "is_premium": false
}
```

> Cache this list client-side. It doesn't change frequently.

---

## 7. Audio Storage

All generated audio is stored as binary data (BLOB) in the server's SQLite database. There are no public file URLs — always access audio through the endpoints below using the `generation_id` (integer).

### Key rule: Download = Delete

```
GET /api/audio/{id}           → streams audio, keeps it on server
GET /api/audio/{id}/download  → serves audio AND deletes it from server
```

Once downloaded to the user's device, the audio is **permanently removed from the server**. Design your UI accordingly — show a clear "Save to device" action that is distinct from "Play".

---

### GET `/api/audio/{generation_id}`

Stream audio for in-app playback.

**Auth:** Required
(Also accepts `?token=<access_token>` query param for media players that can't set headers)

**Path param:** `generation_id` — integer from `X-Generation-Id` header or history `items[].id`

**Response `200`:** Binary audio bytes
```
Content-Type: audio/mpeg  |  audio/wav
Content-Disposition: inline; filename=output.mp3
```

**Errors:** `401` · `404` not found or already downloaded/deleted

---

### GET `/api/audio/{generation_id}/download`

Export audio to the device. **Deletes record from server after serving.**

**Auth:** Required (also accepts `?token=` query param)

**Response `200`:** Binary audio bytes
```
Content-Type: audio/mpeg  |  audio/wav
Content-Disposition: attachment; filename=output_42.mp3
```

> After this call returns `200`, calling `/api/audio/{id}` again returns `404`. There is no undo.

**Errors:** `401` · `404` already exported or never existed

---

### DELETE `/api/audio/{generation_id}`

Discard audio without saving it to device.

**Auth:** Required

**Response `200`:**
```json
{ "success": true, "deleted_id": 42 }
```

**Errors:** `403` not your audio · `404` not found

---

## 8. Generation History

Every generation (TTS, music, song, voice clone, enhance) is recorded. Each history item's `id` is the `generation_id` you use with the audio endpoints.

### GET `/api/history`

Paginated list of the user's generations.

**Auth:** Required

**Query params:**

| Param | Default | Max |
|---|---|---|
| `page` | `1` | — |
| `per_page` | `20` | `100` |

**Response `200`:**
```json
{
  "page": 1,
  "per_page": 20,
  "total": 50,
  "total_pages": 3,
  "items": [
    {
      "id": 42,
      "model": "en-US-AriaNeural",
      "text_snippet": "Hello, world!",
      "audio_format": "mp3",
      "created_at": "2025-01-15T10:30:00"
    }
  ]
}
```

> `items[].id` is the `generation_id`. Use it with `/api/audio/{id}`.
> If audio was already downloaded (exported), `/api/audio/{id}` returns `404` for that item.

---

### DELETE `/api/history/{entry_id}`

Delete one history entry and its audio.

**Auth:** Required

**Response `200`:** `{ "success": true, "deleted_id": 42 }`

**Errors:** `404` not found

---

### DELETE `/api/history`

Clear all history for the current user.

**Auth:** Required

**Response `200`:** `{ "success": true, "deleted_count": 15 }`

---

## 9. Voice Cloning

Clone a voice from a short audio sample and generate speech with it using XTTS v2.

> **These endpoints use the async job system.** They return a `job_id` immediately.

---

### POST `/api/voice_clone/upload`

Upload a voice sample to create a cloned voice profile.

**Auth:** Required | **Rate limit:** 10 req/min | **Content-Type:** `multipart/form-data`

| Field | Rules |
|---|---|
| `file` | wav / mp3 / ogg / flac / m4a · 3–30 seconds · max 10 MB |
| `name` | 1–60 chars · alphanumeric, spaces, hyphens, underscores |

**Response `200`:**
```json
{
  "success": true,
  "voice_id": 5,
  "name": "My Voice",
  "duration_seconds": 12.4,
  "created_at": "2025-01-15T10:30:00"
}
```

> Free users: max **10** cloned voices total.

**Errors:** `400` bad format/duration/size · `403` limit reached · `429` rate limited

---

### GET `/api/voice_clone/list`

List all uploaded voice profiles.

**Auth:** Required

**Response `200`:**
```json
{
  "voices": [
    {
      "id": 5,
      "name": "My Voice",
      "reference_filename": "sample_abc.wav",
      "created_at": "2025-01-15T10:30:00"
    }
  ],
  "count": 1,
  "limit": 10
}
```

---

### DELETE `/api/voice_clone/{voice_id}`

Delete a cloned voice profile.

**Auth:** Required

**Response `200`:** `{ "success": true, "deleted_id": 5 }`

---

### POST `/api/voice_clone/generate`

Generate speech using a saved cloned voice. **Returns job immediately.**

**Auth:** Required

**Request:**
```json
{
  "voice_id": 5,
  "text": "This is my cloned voice.",
  "language": "en"
}
```

**Supported languages:** `ar` `cs` `de` `en` `es` `fr` `hi` `hu` `it` `ja` `ko` `nl` `pl` `pt` `ru` `tr` `zh-cn`

**Response `200`:**
```json
{
  "job_id": "a1b2c3d4e5f6g7h8",
  "status": "pending",
  "poll_url": "/api/jobs/a1b2c3d4e5f6g7h8",
  "voice_name": "My Voice",
  "language": "en"
}
```

→ Poll `/api/jobs/{job_id}` until `status == "done"`, then use `audio_url`.

**Job timeout:** 50 seconds

**Errors:** `400` bad text/language · `404` voice not found

---

### GET `/api/voice_clone/languages`

List languages supported by the voice cloning engine.

**Auth:** Required

**Response `200`:**
```json
{
  "languages": ["ar", "cs", "de", "en", "es", "fr", "hi", "hu", "it", "ja", "ko", "nl", "pl", "pt", "ru", "tr", "zh-cn"]
}
```

---

## 10. Voice Profiles

A more persistent form of cloned voice with higher per-user limits on premium.

---

### POST `/api/voice_clone/save_profile`

Save a reference audio as a reusable voice profile.

**Auth:** Required | **Content-Type:** `multipart/form-data`

| Field | Rules |
|---|---|
| `name` | max 50 chars |
| `reference_audio` | mp3 / wav / ogg / m4a / flac · 6–30s recommended · max 25 MB |

**Limits:** Free = 3 profiles · Premium = 20 profiles

**Response `200`:**
```json
{
  "success": true,
  "profile_id": 2,
  "name": "Studio Voice",
  "created_at": "2025-01-15T10:30:00"
}
```

**Errors:** `400` validation · `403` limit reached

---

### GET `/api/voice_clone/profiles`

**Auth:** Required

**Response `200`:**
```json
{
  "profiles": [
    { "id": 2, "name": "Studio Voice", "created_at": "2025-01-15T10:30:00" }
  ],
  "count": 1,
  "max_profiles": 3
}
```

---

### DELETE `/api/voice_clone/profiles/{profile_id}`

**Auth:** Required

**Response `200`:** `{ "success": true, "deleted_id": 2 }`

---

### POST `/api/voice_clone/from_profile/{profile_id}`

Generate speech using a saved profile. **Returns job immediately.**

**Auth:** Required

**Request:**
```json
{ "text": "Hello from my saved profile.", "language": "en" }
```

**Response `200`:**
```json
{
  "job_id": "b2c3d4e5f6g7h8i9",
  "status": "pending",
  "poll_url": "/api/jobs/b2c3d4e5f6g7h8i9"
}
```

→ Poll until done.

**Job timeout:** 50 seconds

---

## 11. Audio Enhancement

### POST `/api/enhance_audio`

Apply audio effects to an uploaded file. **Synchronous** — returns audio bytes directly.

**Auth:** Required | **Content-Type:** `multipart/form-data`

| Field | Type | Range | Default |
|---|---|---|---|
| `file` | audio | wav/mp3/ogg/flac/m4a · max 50 MB | required |
| `normalize` | bool | — | `true` |
| `fade_in` | float | 0–5 s | `0` |
| `fade_out` | float | 0–5 s | `0` |
| `reverb_amount` | float | 0.0–1.0 | `0` |
| `pitch_steps` | int | -6 to +6 | `0` |
| `speed_factor` | float | 0.5–2.0 | `1.0` |

**Response `200`:** Binary WAV audio bytes

| Header | Value |
|---|---|
| `Content-Type` | `audio/wav` |
| `X-Generation-Id` | integer — save for later streaming/download |

---

## 12. Speech-to-Text

### POST `/api/transcribe`

Transcribe audio using local OpenAI Whisper. **Synchronous.**

**Auth:** Required | **Content-Type:** `multipart/form-data`

| Field | Values | Default |
|---|---|---|
| `file` | mp3/wav/ogg/m4a/mp4/flac/webm/opus · max 50 MB | required |
| `model_size` | `tiny` · `base` · `small` | `base` |
| `language` | BCP-47 code (`en`, `es`, `fr`…) or `auto` | `auto` |

**Response `200`:**
```json
{
  "success": true,
  "text": "The full transcribed text.",
  "segments": [
    { "start": 0.0, "end": 2.5, "text": "The full" },
    { "start": 2.5, "end": 4.1, "text": "transcribed text." }
  ],
  "model": "openai/whisper-base",
  "language_hint": "en",
  "word_count": 4,
  "character_count": 24,
  "segment_count": 2
}
```

**Errors:** `400` bad format/size · `500` model error

---

## 13. Music Generation

### POST `/api/generate_music`

Generate instrumental music from a text prompt. **Returns job immediately.**

**Auth:** Required

**Request:**
```json
{
  "prompt": "upbeat jazz piano with light drums",
  "duration": 8
}
```

| Field | Limit | Default |
|---|---|---|
| `prompt` | max 1000 chars | required |
| `duration` | seconds (capped server-side at 8s max) | `10` → capped to `8` |

**Response `200`:**
```json
{
  "job_id": "c3d4e5f6g7h8i9j0",
  "status": "pending",
  "poll_url": "/api/jobs/c3d4e5f6g7h8i9j0"
}
```

→ Poll until done, then use `audio_url` (WAV).

**Job timeout:** 60 seconds

---

## 14. Song Generation

### POST `/api/generate_song`

Generate a song with vocals and background music. Vocals use edge-tts (fast), background uses MusicGen (4s clip). **Returns job immediately.**

**Auth:** Required

**Request:**
```json
{
  "lyrics": "Under the stars we dance tonight\nFeelings so warm in the pale moonlight",
  "voice_preset": "en_singer_3",
  "style": "pop",
  "quality": "small"
}
```

**Voice presets:**

| Preset | Voice |
|---|---|
| `en_singer_1` | English Male 1 |
| `en_singer_2` | English Male 2 |
| `en_singer_3` | English Female 1 |
| `en_singer_4` | English Female 2 |
| `en_female_1` | English Female 3 |
| `en_female_2` | English Female 4 |
| `es_singer_1` | Spanish Female |
| `fr_singer_1` | French Female |
| `de_singer_1` | German Female |
| `hi_singer_1` | Hindi Female |
| `tr_singer_1` | Turkish Female |
| `ru_singer_1` | Russian Female |
| `pt_singer_1` | Portuguese Female |
| `zh_singer_1` | Chinese Female |
| `ar_singer_1` | Arabic Female |

**Styles:** `pop` · `ballad` · `hiphop` · `rock` · `jazz` · `rnb` · `electronic` · `acoustic`

**Quality:** `small` (faster) · `large` (higher quality, slower)

**Response `200`:**
```json
{
  "job_id": "d4e5f6g7h8i9j0k1",
  "status": "pending",
  "poll_url": "/api/jobs/d4e5f6g7h8i9j0k1"
}
```

→ Poll until done, then use `audio_url` (WAV).

**Job timeout:** 90 seconds

---

### GET `/api/song/voices`

List all voice presets and styles.

**Auth:** Required

**Response `200`:**
```json
{
  "voices": [
    { "id": "en_singer_1", "bark_preset": "v2/en_speaker_1", "label": "En Singer 1" }
  ],
  "styles": [
    { "id": "pop", "label": "Pop" }
  ]
}
```

---

## 15. Translation

### POST `/api/translate`

**Auth:** Required | Max 5000 chars

**Request:**
```json
{ "text": "Hello, how are you?", "target_lang": "es" }
```

**Response `200`:**
```json
{ "success": true, "translated_text": "Hola, ¿cómo estás?" }
```

**Errors:** `400` empty text / unsupported language · `500` service error

---

### GET `/api/languages`

All supported translation language codes. **Public endpoint.**

**Response `200`:**
```json
{
  "success": true,
  "languages": { "en": "english", "es": "spanish", "fr": "french" }
}
```

> 100+ languages. Call once at app startup and cache.

---

## 16. User Stats

### GET `/api/stats`

**Auth:** Required

**Response `200`:**
```json
{
  "plan": "free",
  "generation_count": 20,
  "generations_remaining": 30,
  "credits": 30,
  "is_premium": false,
  "total_in_history": 18,
  "last_generation": "2025-01-15T10:30:00",
  "top_voices": [
    { "model": "en-US-AriaNeural", "count": 8 }
  ],
  "member_since": "2025-01-01T00:00:00"
}
```

> `credits: -1` means unlimited (premium user).

---

## 17. Error Reference

### Standard error shape

```json
{ "detail": "Human-readable description of the error." }
```

### HTTP status codes

| Code | Meaning | Common causes |
|---|---|---|
| `400` | Bad request | Empty text, unsupported format, missing field, value out of range |
| `401` | Unauthorized | Missing token, expired token, wrong token type |
| `403` | Forbidden | Accessing another user's audio, profile limit reached |
| `404` | Not found | Audio already downloaded/deleted, job/entry not found |
| `409` | Conflict | Email already registered |
| `429` | Rate limited | Login (10/min), TTS (20/min), voice upload (10/min) |
| `500` | Server error | AI model crash, subprocess timed out, dependency missing |

### Handling `404` on audio endpoints

A `404` on `GET /api/audio/{id}` means one of:
- The audio was already exported via the `/download` endpoint
- It was manually deleted
- The job failed before saving audio

Check the history item first — if it exists in `/api/history` but audio returns `404`, it means it was already exported to a device.

---

## 18. Timeouts Reference

### Subprocess timeouts (synchronous endpoints)

| Operation | Timeout | On breach |
|---|---|---|
| edge-tts (TTS generation) | 30s | HTTP 500 returned immediately |
| piper CLI (TTS generation) | 30s | HTTP 500 returned immediately |
| edge-tts (song vocals step) | 30s | Job marked `failed` |

### Job timeouts (async endpoints)

| Job type | Timeout | `error_message` on breach |
|---|---|---|
| Music | 60s | `"Generation timed out after 60s"` |
| Song | 90s | `"Generation timed out after 90s"` |
| Voice clone | 50s | `"Generation timed out after 50s"` |

### Client-side polling timeout (your responsibility)

Set a maximum poll duration in your app. Recommended:

| Job type | Max client poll time |
|---|---|
| Music | 90s (30 polls × 3s) |
| Song | 120s (40 polls × 3s) |
| Voice clone | 90s (30 polls × 3s) |

If the server times out the job, your next poll returns `status: "failed"` instantly.

---

## 19. Constants Reference

| Constant | Value |
|---|---|
| Free user starting credits | 50 |
| Access token lifetime | 24 hours |
| Refresh token lifetime | 90 days |
| Max TTS text | 5,000 chars |
| Max music prompt | 1,000 chars |
| Max song lyrics | 5,000 chars |
| Max audio upload (enhance / transcribe) | 50 MB |
| Max voice clone sample | 10 MB · 3–30 seconds |
| Max reference audio (profiles) | 25 MB |
| Max cloned voices per user (free) | 10 |
| Voice profiles (free / premium) | 3 / 20 |
| Music max generated duration | 8 seconds |
| Background job workers | 3 concurrent |

---

## 20. Response Times

### Synchronous endpoints (always fast)

| Endpoint | Typical time |
|---|---|
| Auth endpoints | < 150ms |
| Voices, history, stats, jobs | < 50ms |
| TTS with edge-tts | 1–4s |
| TTS with piper (cached model) | 0.5–2s |
| Audio stream / download | 50–300ms |
| Enhance audio | 1–3s |
| Transcribe (Whisper base) | 4–8s |
| Translate | 0.5–2s |

### Async job completion times

| Job | CPU (8-core, no GPU) | GPU (CUDA) |
|---|---|---|
| Music (8s clip) | 25–55s | 5–10s |
| Song (edge-tts + 4s background) | 25–40s | 8–15s |
| Voice clone (short text) | 15–25s | 5–10s |

> These times apply after models are warmed up. The server pre-warms all models at startup in background threads. First request after a cold start may be slower.

---

## Full Example Flow — Song Generation

```
1. POST /api/auth/login
   ← { access_token, refresh_token }
   → Store both tokens securely

2. POST /api/generate_song
   Body: { lyrics, voice_preset, style, quality }
   ← { job_id: "abc123", status: "pending" }
   → Show loading UI

3. loop every 3s:
   GET /api/jobs/abc123
   ← { status: "processing" }     → keep spinner
   ← { status: "done",
       generation_id: 42,
       audio_url: "/api/audio/42" } → stop spinner

4. GET /api/audio/42
   ← WAV audio bytes
   → Play in-app audio player

5. User taps "Save to Device":
   GET /api/audio/42/download
   ← WAV audio bytes (server deletes record)
   → Save file locally
   → Show "Saved!" confirmation

6. User taps "Discard":
   DELETE /api/audio/42
   ← { success: true }
   → Remove from UI
```

---

*Pollux Backend v2.0.0 — Last updated 2026-04-23*
