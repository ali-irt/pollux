# Pollux API — Mobile Developer Documentation

> **Base URL:** `http://<your-server>:8000`
> **Framework:** FastAPI — interactive docs available at `/docs`

---

## Table of Contents

1. [Authentication](#1-authentication)
2. [Text-to-Speech](#2-text-to-speech)
3. [Voices](#3-voices)
4. [Audio Storage](#4-audio-storage)
5. [Generation History](#5-generation-history)
6. [Voice Cloning](#6-voice-cloning)
7. [Voice Profiles](#7-voice-profiles)
8. [Audio Enhancement](#8-audio-enhancement)
9. [Speech-to-Text](#9-speech-to-text)
10. [Music Generation](#10-music-generation)
11. [Song Generation](#11-song-generation)
12. [Translation](#12-translation)
13. [User Stats](#13-user-stats)
14. [Error Reference](#14-error-reference)
15. [Constants Reference](#15-constants-reference)

---

## 1. Authentication

All protected endpoints require a **Bearer token** in the `Authorization` header:

```
Authorization: Bearer <access_token>
```

Tokens are JWT (HS256). Access tokens expire in **24 hours**; refresh tokens last **90 days**.

---

### POST `/api/auth/register`

Create a new account. Returns tokens immediately.

**Auth:** None

**Request:**
```json
{
  "email": "user@example.com",
  "password": "StrongPass1!"
}
```

**Password rules:**
- 8–56 characters
- At least one uppercase letter (A–Z)
- At least one lowercase letter (a–z)
- At least one digit (0–9)
- At least one special character (`!@#$%^&*...`)

**Response `200`:**
```json
{
  "access_token": "<jwt>",
  "refresh_token": "<jwt>",
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

**Auth:** None | **Rate limit:** 10/min

**Request:**
```json
{
  "email": "user@example.com",
  "password": "StrongPass1!"
}
```

**Response `200`:** Same shape as register response.

> After 5 failed attempts within 15 minutes the account is temporarily locked.

**Errors:** `401` invalid credentials · `429` rate limit

---

### POST `/api/auth/refresh`

Exchange a refresh token for a new access token.

**Auth:** None

**Request:**
```json
{
  "refresh_token": "<jwt>"
}
```

**Response `200`:**
```json
{
  "access_token": "<jwt>",
  "token_type": "bearer"
}
```

**Errors:** `401` invalid or expired refresh token

---

### GET `/api/auth/me`

Get the current authenticated user's profile.

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

**Response `200`:**
```json
{
  "success": true,
  "message": "Password updated successfully."
}
```

**Errors:** `400` weak new password · `401` wrong current password

---

### POST `/api/auth/upgrade-to-premium`

Upgrade the current user to premium (unlimited credits).

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "message": "Successfully upgraded to premium.",
  "plan": "premium",
  "is_premium": true,
  "credits": -1
}
```

> `credits: -1` means unlimited.

---

## 2. Text-to-Speech

### POST `/api/generate`

Generate speech from text.

**Auth:** Required | **Rate limit:** 20/min

**Request:**
```json
{
  "text": "Hello, world!",
  "model": "en-US-AriaNeural",
  "speed": 1.0,
  "pitch_hz": 0
}
```

| Field | Type | Range | Default | Notes |
|---|---|---|---|---|
| `text` | string | max 5000 chars | — | Required |
| `model` | string | — | — | Voice ID from `/api/voices` |
| `speed` | float | 0.5–2.0 | `1.0` | Playback speed |
| `pitch_hz` | int | -10 to +10 | `0` | Pitch shift in Hz |

**Response `200`:** Binary audio bytes

| Header | Value |
|---|---|
| `Content-Type` | `audio/mpeg` (edge-tts) or `audio/wav` (piper) |
| `Content-Disposition` | `inline; filename=output.mp3` |
| `X-Generation-Id` | e.g. `42` — use this ID for streaming or downloading later |

> Save the `X-Generation-Id` from the response header. Use it to stream, download, or delete the audio later.

**Errors:** `400` empty text / unknown model · `429` rate limit · `500` synthesis error

---

## 3. Voices

### GET `/api/voices`

All available voices with premium flags.

**Auth:** Required

**Response `200`:**
```json
[
  {
    "name": "en-US-AriaNeural",
    "language": "en-US",
    "engine": "edge-tts",
    "is_premium": false
  }
]
```

---

### GET `/api/voices/free`

Free-tier voices only.

**Auth:** Required

**Response `200`:** Same array shape, only non-premium entries.

---

### GET `/api/voices/premium`

Premium voices only.

**Auth:** Required

**Response `200`:** Same array shape, only premium entries.

---

## 4. Audio Storage

All generated audio is stored as binary data in the server's SQLite database, keyed by a **generation ID** (integer). There is no public file path — always use the endpoints below.

> **Export = Delete:** Calling the `/download` endpoint serves the audio **and permanently removes it from the database**. This is by design — once the user saves the file to their device, it is cleared from server storage.

---

### GET `/api/audio/{generation_id}`

Stream audio from the database (for in-app playback).

**Auth:** Required (Bearer header or `?token=<jwt>` query param)

**Path param:** `generation_id` — integer returned in `X-Generation-Id` header or history items.

**Response `200`:** Binary audio bytes
```
Content-Type: audio/mpeg  (mp3)  |  audio/wav
Content-Disposition: inline; filename=output.mp3
```

**Errors:** `401` unauthenticated · `404` not found (may have already been exported)

---

### GET `/api/audio/{generation_id}/download`

Export audio to the user's device. **Deletes the record from DB after serving.**

**Auth:** Required (Bearer header or `?token=<jwt>` query param)

**Response `200`:** Binary audio bytes
```
Content-Type: audio/mpeg  |  audio/wav
Content-Disposition: attachment; filename=output_42.mp3
```

> After this call succeeds, the audio no longer exists on the server. The history entry is also removed.

**Errors:** `401` · `404` already exported or never existed

---

### DELETE `/api/audio/{generation_id}`

Delete audio from the database without downloading it.

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "deleted_id": 42
}
```

**Errors:** `403` not your audio · `404` not found

---

## 5. Generation History

History entries are stored per-user. Each entry has an `id` field which maps directly to the audio `generation_id` used in section 4.

---

### GET `/api/history`

Paginated list of the user's past generations.

**Auth:** Required

**Query params:**

| Param | Type | Default | Max |
|---|---|---|---|
| `page` | int | `1` | — |
| `per_page` | int | `20` | `100` |

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
      "user_id": 1,
      "filename": "",
      "model": "en-US-AriaNeural",
      "text_snippet": "Hello, world!",
      "audio_format": "mp3",
      "created_at": "2025-01-15T10:30:00"
    }
  ]
}
```

> `id` in each item is the `generation_id` — use it directly with `/api/audio/{id}`.
> Items with `audio_data` already exported (downloaded) will return `404` from the audio endpoints.

---

### DELETE `/api/history/{entry_id}`

Delete a single history entry (and its stored audio).

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "deleted_id": 42
}
```

**Errors:** `404` entry not found

---

### DELETE `/api/history`

Clear all history for the current user.

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "deleted_count": 15
}
```

---

## 6. Voice Cloning

Clone a voice from a short audio sample and generate speech with it.

---

### POST `/api/voice_clone/upload`

Upload a voice sample to create a cloned voice profile.

**Auth:** Required | **Rate limit:** 10/min | **Content-Type:** `multipart/form-data`

| Field | Type | Rules |
|---|---|---|
| `file` | audio file | wav/mp3/ogg/flac/m4a · 3–30 seconds · max 10 MB |
| `name` | string | 1–60 chars · alphanumeric, spaces, hyphens, underscores |

**Response `200`:**
```json
{
  "success": true,
  "voice_id": 5,
  "name": "My Voice",
  "duration_seconds": 12.4,
  "filename": "sample_abc123.wav",
  "created_at": "2025-01-15T10:30:00"
}
```

**Errors:** `400` bad format / duration / size · `403` profile limit reached · `429` rate limit

> Free users: max **10** cloned voices.

---

### GET `/api/voice_clone/list`

List all uploaded voice profiles for the current user.

**Auth:** Required

**Response `200`:**
```json
{
  "voices": [
    {
      "id": 5,
      "user_id": 1,
      "name": "My Voice",
      "reference_filename": "sample_abc123.wav",
      "created_at": "2025-01-15T10:30:00"
    }
  ],
  "count": 1,
  "limit": 10
}
```

---

### DELETE `/api/voice_clone/{voice_id}`

Delete a cloned voice profile and its reference audio.

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "deleted_id": 5
}
```

**Errors:** `404` not found

---

### POST `/api/voice_clone/generate`

Generate speech using a saved cloned voice (XTTS v2 engine).

**Auth:** Required

**Request:**
```json
{
  "voice_id": 5,
  "text": "This is my cloned voice speaking.",
  "language": "en"
}
```

**Supported languages:** `ar`, `cs`, `de`, `en`, `es`, `fr`, `hi`, `hu`, `it`, `ja`, `ko`, `nl`, `pl`, `pt`, `ru`, `tr`, `zh-cn`

**Response `200`:**
```json
{
  "success": true,
  "generation_id": 43,
  "audio_url": "/api/audio/43",
  "download_url": "/api/audio/43/download",
  "voice_name": "My Voice",
  "language": "en",
  "credits": 40,
  "generations_remaining": 40
}
```

> Use `audio_url` for playback and `download_url` to export (which deletes from DB).

**Errors:** `400` bad text/language · `404` voice not found · `500` XTTS error

---

### GET `/api/voice_clone/languages`

List all languages supported by the voice cloning engine.

**Auth:** Required

**Response `200`:**
```json
{
  "languages": ["ar", "cs", "de", "en", "es", "fr", "hi", "hu", "it", "ja", "ko", "nl", "pl", "pt", "ru", "tr", "zh-cn"]
}
```

---

## 7. Voice Profiles

Saved profiles are a more persistent form of cloned voice, with separate limits.

---

### POST `/api/voice_clone/save_profile`

Save a reference audio as a reusable voice profile.

**Auth:** Required | **Content-Type:** `multipart/form-data`

| Field | Type | Rules |
|---|---|---|
| `name` | string | max 50 chars |
| `reference_audio` | audio file | mp3/wav/ogg/m4a/flac · 6–30 s recommended · max 25 MB |

**Profile limits:**

| Plan | Max profiles |
|---|---|
| Free | 3 |
| Premium | 20 |

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

List saved voice profiles.

**Auth:** Required

**Response `200`:**
```json
{
  "profiles": [
    {
      "id": 2,
      "name": "Studio Voice",
      "reference_filename": "profile_abc.wav",
      "created_at": "2025-01-15T10:30:00"
    }
  ],
  "count": 1,
  "max_profiles": 3
}
```

---

### DELETE `/api/voice_clone/profiles/{profile_id}`

Delete a saved voice profile.

**Auth:** Required

**Response `200`:**
```json
{
  "success": true,
  "deleted_id": 2
}
```

---

### POST `/api/voice_clone/from_profile/{profile_id}`

Generate speech using a saved voice profile.

**Auth:** Required

**Request:**
```json
{
  "text": "Hello from my saved profile.",
  "language": "en"
}
```

**Response `200`:** Binary WAV audio bytes

| Header | Value |
|---|---|
| `Content-Type` | `audio/wav` |
| `Content-Disposition` | `inline; filename=clone.wav` |
| `X-Generation-Id` | Integer — use for streaming/downloading |

**Errors:** `400` bad text/language · `404` profile not found · `500` XTTS error

---

## 8. Audio Enhancement

### POST `/api/enhance_audio`

Apply audio effects to an uploaded file. Returns the enhanced WAV.

**Auth:** Required | **Content-Type:** `multipart/form-data`

| Field | Type | Range | Default |
|---|---|---|---|
| `file` | audio file | wav/mp3/ogg/flac/m4a · max 50 MB | — |
| `normalize` | boolean | — | `true` |
| `fade_in` | float | 0–5 seconds | `0` |
| `fade_out` | float | 0–5 seconds | `0` |
| `reverb_amount` | float | 0.0–1.0 | `0` |
| `pitch_steps` | int | -6 to +6 | `0` |
| `speed_factor` | float | 0.5–2.0 | `1.0` |

**Response `200`:** Binary WAV audio bytes

| Header | Value |
|---|---|
| `Content-Type` | `audio/wav` |
| `Content-Disposition` | `inline; filename=enhanced.wav` |
| `X-Generation-Id` | Integer — use for later streaming/downloading |

**Errors:** `400` bad format / file too large · `500` processing error

---

## 9. Speech-to-Text

### POST `/api/transcribe`

Transcribe an audio file to text using OpenAI Whisper (runs fully locally).

**Auth:** Required | **Content-Type:** `multipart/form-data`

| Field | Values | Default |
|---|---|---|
| `file` | mp3/wav/ogg/m4a/mp4/flac/webm/opus · max 50 MB | — |
| `model_size` | `tiny` · `base` · `small` | `base` |
| `language` | BCP-47 code (`en`, `es`, `fr`...) or `auto` | `auto` |

**Response `200`:**
```json
{
  "success": true,
  "text": "The full transcribed text goes here.",
  "segments": [
    {
      "start": 0.0,
      "end": 2.5,
      "text": "The full transcribed"
    },
    {
      "start": 2.5,
      "end": 4.1,
      "text": "text goes here."
    }
  ],
  "model": "openai/whisper-base",
  "language_hint": "en",
  "word_count": 6,
  "character_count": 34,
  "segment_count": 2
}
```

**Errors:** `400` unsupported format / file too large · `500` model error

---

## 10. Music Generation

### POST `/api/generate_music`

Generate instrumental music from a text prompt using MusicGen (fully local, no external API).

**Auth:** Required

**Request:**
```json
{
  "prompt": "upbeat jazz piano with light drums",
  "duration": 10
}
```

| Field | Type | Limit | Default |
|---|---|---|---|
| `prompt` | string | max 1000 chars | — |
| `duration` | int (seconds) | — | `10` |

**Response `200`:** Binary WAV audio bytes

| Header | Value |
|---|---|
| `Content-Type` | `audio/wav` |
| `Content-Disposition` | `inline; filename=music.wav` |
| `X-Generation-Id` | Integer |

**Errors:** `400` prompt too long · `500` model error

---

## 11. Song Generation

### POST `/api/generate_song`

Generate a full AI song (vocals + background music) from lyrics. Uses Bark for vocals and MusicGen for the background track.

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

| Language | Presets |
|---|---|
| English | `en_singer_1`, `en_singer_2`, `en_singer_3`, `en_singer_4`, `en_female_1`, `en_female_2` |
| Spanish | `es_singer_1` |
| French | `fr_singer_1` |
| German | `de_singer_1` |
| Hindi | `hi_singer_1` |
| Turkish | `tr_singer_1` |
| Russian | `ru_singer_1` |
| Portuguese | `pt_singer_1` |
| Chinese | `zh_singer_1` |
| Arabic | `ar_singer_1` |

**Styles:** `pop` · `ballad` · `hiphop` · `rock` · `jazz` · `rnb` · `electronic` · `acoustic`

**Quality:** `small` (faster) · `large` (higher quality, slower)

**Response `200`:** Binary WAV audio bytes

| Header | Value |
|---|---|
| `Content-Type` | `audio/wav` |
| `Content-Disposition` | `inline; filename=song.wav` |
| `X-Generation-Id` | Integer |

**Errors:** `400` invalid preset/style · `500` model error

---

### GET `/api/song/voices`

List all available voice presets and styles for song generation.

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

## 12. Translation

### POST `/api/translate`

Translate text to a target language using Google Translate.

**Auth:** Required

**Request:**
```json
{
  "text": "Hello, how are you?",
  "target_lang": "es"
}
```

Max text length: 5000 characters.

**Response `200`:**
```json
{
  "success": true,
  "translated_text": "Hola, ¿cómo estás?"
}
```

**Errors:** `400` empty text / unsupported language · `500` service error

---

### GET `/api/languages`

Get all supported translation language codes and names.

**Auth:** None (public)

**Response `200`:**
```json
{
  "success": true,
  "languages": {
    "en": "english",
    "es": "spanish",
    "fr": "french"
  }
}
```

> Returns 100+ languages. Call once and cache client-side.

---

## 13. User Stats

### GET `/api/stats`

Get usage statistics for the current user.

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

---

## 14. Error Reference

| Code | Meaning |
|---|---|
| `400` | Bad request — validation failed, unsupported format, or missing required field |
| `401` | Unauthorized — missing, expired, or invalid token |
| `403` | Forbidden — you do not own this resource, or a limit has been reached |
| `404` | Not found — resource does not exist, or audio was already exported/deleted |
| `409` | Conflict — email already registered |
| `429` | Rate limit exceeded — wait and retry |
| `500` | Server error — AI model failure or processing error |

**Error response shape:**
```json
{
  "detail": "Human-readable description of what went wrong."
}
```

---

## 15. Constants Reference

| Constant | Value |
|---|---|
| Free user starting credits | 50 |
| Access token lifetime | 24 hours |
| Refresh token lifetime | 90 days |
| Max TTS / translation text | 5,000 chars |
| Max music prompt | 1,000 chars |
| Max song lyrics | 5,000 chars |
| Max audio upload (enhance / transcribe) | 50 MB |
| Max voice clone sample | 10 MB · 3–30 seconds |
| Max reference audio (profiles) | 25 MB |
| Cloned voices per user | 10 |
| Voice profiles (free / premium) | 3 / 20 |

---

## Typical Mobile Flow

```
1.  Register / Login  →  store access_token + refresh_token
2.  GET /api/voices/free  →  show voice picker
3.  POST /api/generate  →  play audio bytes in-app
                            save X-Generation-Id header
4.  GET /api/history  →  show library (items[].id = generation_id)
5.  GET /api/audio/{id}  →  re-play from DB
6.  GET /api/audio/{id}/download  →  save to device  (removes from server)
7.  DELETE /api/audio/{id}  →  discard without saving
8.  POST /api/auth/refresh  →  when access_token expires
```

---

*Generated for Pollux backend v2.0.0*
