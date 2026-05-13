#!/usr/bin/env bash
# test_voxcpm_api.sh
# Tests all four /voice endpoints against the running VoiceWave server.
#
# Usage:
#   chmod +x test_voxcpm_api.sh
#   ./test_voxcpm_api.sh [BASE_URL] [EMAIL] [PASSWORD]
#
# Defaults:
#   BASE_URL  = http://localhost:8004
#   EMAIL     = test@example.com
#   PASSWORD  = TestPass123!
#
# Requires: curl, jq

set -euo pipefail

BASE="${1:-http://localhost:8004}"
EMAIL="${2:-test@example.com}"
PASS="${3:-TestPass123!}"
REF_AUDIO="/var/www/voicewave/voice/reference.wav"
OUT_DIR="/tmp/voxcpm_test"
mkdir -p "$OUT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

# ---------------------------------------------------------------------------
# 0. Ensure reference audio exists
# ---------------------------------------------------------------------------
if [[ ! -f "$REF_AUDIO" ]]; then
    info "reference.wav not found at $REF_AUDIO — generating a 3s silent WAV for testing"
    python3 -c "
import wave, struct, math
with wave.open('$OUT_DIR/reference.wav','w') as f:
    f.setnchannels(1); f.setsampwidth(2); f.setframerate(22050)
    for i in range(22050*3):
        f.writeframes(struct.pack('<h', int(3000*math.sin(2*math.pi*440*i/22050))))
"
    REF_AUDIO="$OUT_DIR/reference.wav"
fi

# ---------------------------------------------------------------------------
# 1. Login — get access token
# ---------------------------------------------------------------------------
info "Logging in as $EMAIL ..."
LOGIN=$(curl -sf -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" || true)

TOKEN=$(echo "$LOGIN" | jq -r '.access_token // empty' 2>/dev/null)
if [[ -z "$TOKEN" ]]; then
    info "Login failed — attempting registration first ..."
    curl -sf -X POST "$BASE/api/auth/register" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" > /dev/null || true
    LOGIN=$(curl -sf -X POST "$BASE/api/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
    TOKEN=$(echo "$LOGIN" | jq -r '.access_token')
fi

if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
    fail "Could not obtain access token. Check credentials or server status."
    exit 1
fi
pass "Login OK — token obtained"
AUTH="Authorization: Bearer $TOKEN"

# ---------------------------------------------------------------------------
# 2. GET /voice/health
# ---------------------------------------------------------------------------
info "Testing GET /voice/health ..."
HTTP=$(curl -s -o /tmp/voxcpm_health.json -w "%{http_code}" \
    -H "$AUTH" "$BASE/voice/health")
BODY=$(cat /tmp/voxcpm_health.json)
if [[ "$HTTP" == "200" ]]; then
    pass "/voice/health → 200  |  $BODY"
elif [[ "$HTTP" == "503" ]]; then
    fail "/voice/health → 503 (VoxCPM server not reachable — is voxcpm.service running?)"
    fail "Body: $BODY"
    exit 1
else
    fail "/voice/health → $HTTP  |  $BODY"
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. POST /voice/clone
# ---------------------------------------------------------------------------
info "Testing POST /voice/clone ..."
HTTP=$(curl -s -o "$OUT_DIR/clone_output.mp3" -w "%{http_code}" \
    -X POST "$BASE/voice/clone" \
    -H "$AUTH" \
    -F "text=Hello, this is a voice cloning test." \
    -F "reference=@$REF_AUDIO;type=audio/wav")
if [[ "$HTTP" == "200" ]]; then
    SIZE=$(wc -c < "$OUT_DIR/clone_output.mp3")
    pass "/voice/clone → 200  |  ${SIZE} bytes saved to $OUT_DIR/clone_output.mp3"
else
    BODY=$(cat "$OUT_DIR/clone_output.mp3")
    fail "/voice/clone → $HTTP  |  $BODY"
fi

# ---------------------------------------------------------------------------
# 4. POST /voice/transcribe
# ---------------------------------------------------------------------------
info "Testing POST /voice/transcribe ..."
HTTP=$(curl -s -o /tmp/voxcpm_transcribe.json -w "%{http_code}" \
    -X POST "$BASE/voice/transcribe" \
    -H "$AUTH" \
    -F "audio=@$REF_AUDIO;type=audio/wav")
BODY=$(cat /tmp/voxcpm_transcribe.json)
if [[ "$HTTP" == "200" ]]; then
    TEXT=$(echo "$BODY" | jq -r '.text // "(empty)"')
    pass "/voice/transcribe → 200  |  \"$TEXT\""
else
    fail "/voice/transcribe → $HTTP  |  $BODY"
fi

# ---------------------------------------------------------------------------
# 5. POST /voice/diarize
# ---------------------------------------------------------------------------
info "Testing POST /voice/diarize ..."
HTTP=$(curl -s -o /tmp/voxcpm_diarize.json -w "%{http_code}" \
    -X POST "$BASE/voice/diarize" \
    -H "$AUTH" \
    -F "audio=@$REF_AUDIO;type=audio/wav")
BODY=$(cat /tmp/voxcpm_diarize.json)
if [[ "$HTTP" == "200" ]]; then
    TEXT=$(echo "$BODY" | jq -r '.text // "(empty)"')
    SEGS=$(echo "$BODY" | jq '.segments | length')
    pass "/voice/diarize → 200  |  segments=$SEGS  |  \"$TEXT\""
elif [[ "$HTTP" == "502" ]]; then
    info "/voice/diarize → 502 (expected if HF_TOKEN not set — diarization requires pyannote license)"
    info "Body: $BODY"
else
    fail "/voice/diarize → $HTTP  |  $BODY"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
info "Output files in $OUT_DIR:"
ls -lh "$OUT_DIR/"
echo ""
info "To play the cloned audio:  ffplay $OUT_DIR/clone_output.mp3"
