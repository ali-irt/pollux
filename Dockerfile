# Development Dockerfile — hot reload, debug logging
# Usage: docker compose -f docker-compose.dev.yml up
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates \
    libsndfile1 \
    libgomp1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY web_ui/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY web_ui/ ./web_ui/

RUN mkdir -p web_ui/outputs web_ui/voice_samples web_ui/cloned_voices web_ui/db

WORKDIR /app/web_ui

EXPOSE 8004

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004", "--reload"]
