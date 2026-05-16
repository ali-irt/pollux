---
title: VoiceWave ACE-Step Server
emoji: 🎵
colorFrom: purple
colorTo: blue
sdk: gradio
sdk_version: 4.44.1
app_file: app.py
pinned: false
license: apache-2.0
---

# VoiceWave ACE-Step GPU Server

REST API server for ACE-Step 1.5 music generation running on ZeroGPU.

## Endpoints

- `POST /generate` — submit a song generation job
- `GET /result/{job_id}` — poll status / download WAV
- `GET /health` — health check
