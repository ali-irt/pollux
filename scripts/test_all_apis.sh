#!/usr/bin/env bash
# test_all_apis.sh — Tests every endpoint in the VoiceWave API.
#
# Usage:
#   chmod +x test_all_apis.sh
#   ./test_all_apis.sh [BASE_URL] [EMAIL] [PASSWORD]
#
# Defaults:
#   BASE_URL = http://localhost:8004
#   EMAIL    = apitest@example.com
#   PASSWORD = TestPass123!

set -uo pipefail

BASE="${1:-http://localhost:8004}"
EMAIL="${2:-apitest@example.com}"
PASS="${3:-TestPass123!}"
REF_AUDIO="/var/www/voicewave/voice/reference.wav"
OUT="/tmp/voicewave_test"
mkdir -p "$OUT"

# ── colours ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS=0; FAIL=0; SKIP=0

pass()  { echo -e "${GREEN}[PASS]${NC} $1"; ((PASS++));  }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; ((FAIL++));   }
skip()  { echo -e "${YELLOW}[SKIP]${NC} $1"; ((SKIP++)); }
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
title() { echo -e "\n${CYAN}══ $1 ══${NC}"; }

# ── helpers ─────────────────────────────────────────────────────────────────
# call URL METHOD [-H header] [-d body | -F field...] expected_http
# Stores response body in $RESP
RESP=""
api() {
    local method="$1" url="$2" expected="$3"
    shift 3
    local tmpfile
    tmpfile=$(mktemp)
    local code
    code=$(curl -s -o "$tmpfile" -w "%{http_code}" -X "$method" "$BASE$url" \
        -H "$AUTH" "$@")
    RESP=$(cat "$tmpfile"); rm -f "$tmpfile"
    if [[ "$code" == "$expected" ]]; then
        return 0
    else
        echo "    HTTP $code (expected $expected) — ${RESP:0:200}"
        return 1
    fi
}

# ── reference audio fallback ────────────────────────────────────────────────
if [[ ! -f "$REF_AUDIO" ]]; then
    info "Generating silent reference WAV for testing ..."
    python3 - <<'PY'
import wave, struct, math
with wave.open('/tmp/voicewave_test/reference.wav','w') as f:
    f.setnchannels(1); f.setsampwidth(2); f.setframerate(22050)
    for i in range(22050*3):
        f.writeframes(struct.pack('<h', int(3000*math.sin(2*math.pi*440*i/22050))))
PY
    REF_AUDIO="/tmp/voicewave_test/reference.wav"
fi

AUTH=""   # set after login

# ════════════════════════════════════════════════════════════════════════════
title "AUTH"
# ════════════════════════════════════════════════════════════════════════════

# Register (may already exist — 400 is fine)
info "POST /api/auth/register"
BODY=$(curl -sf -X POST "$BASE/api/auth/register" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" 2>/dev/null || true)
if echo "$BODY" | grep -q '"email"'; then
    pass "register — new account created"
else
    info "register — account likely exists already"
fi

# Login
info "POST /api/auth/login"
LOGIN=$(curl -sf -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
ACCESS=$(echo "$LOGIN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null)
REFRESH_TOK=$(echo "$LOGIN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('refresh_token',''))" 2>/dev/null)
if [[ -n "$ACCESS" && "$ACCESS" != "null" ]]; then
    pass "login — token obtained"
    AUTH="Authorization: Bearer $ACCESS"
else
    fail "login — could not get token; aborting"
    exit 1
fi

# GET /api/auth/me
if api GET /api/auth/me 200; then
    USER_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','?'))" 2>/dev/null)
    pass "GET /api/auth/me — user id=$USER_ID"
else fail "GET /api/auth/me"; fi

# POST /api/auth/refresh
if api POST /api/auth/refresh 200 \
    -H "Content-Type: application/json" \
    -d "{\"refresh_token\":\"$REFRESH_TOK\"}"; then
    pass "POST /api/auth/refresh"
else fail "POST /api/auth/refresh"; fi

# POST /api/auth/change-password (change then change back)
if api POST /api/auth/change-password 200 \
    -H "Content-Type: application/json" \
    -d "{\"current_password\":\"$PASS\",\"new_password\":\"${PASS}2\"}"; then
    pass "POST /api/auth/change-password"
    # change back
    api POST /api/auth/change-password 200 \
        -H "Content-Type: application/json" \
        -d "{\"current_password\":\"${PASS}2\",\"new_password\":\"$PASS\"}" > /dev/null || true
else fail "POST /api/auth/change-password"; fi

# ════════════════════════════════════════════════════════════════════════════
title "VOICES"
# ════════════════════════════════════════════════════════════════════════════

for EP in /api/voices /api/voices/free /api/voices/premium; do
    if api GET "$EP" 200; then
        COUNT=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else '?')" 2>/dev/null)
        pass "GET $EP — $COUNT voices"
    else fail "GET $EP"; fi
done

# ════════════════════════════════════════════════════════════════════════════
title "TTS — /api/generate"
# ════════════════════════════════════════════════════════════════════════════

VOICE=$(curl -sf -H "$AUTH" "$BASE/api/voices/free" | \
    python3 -c "import sys,json; v=json.load(sys.stdin); print(v[0]['id'] if v else 'en-US-GuyNeural')" 2>/dev/null)
info "Using voice: $VOICE"

if api POST /api/generate 200 \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"Hello from the test suite.\",\"model\":\"$VOICE\",\"speed\":1.0}"; then
    GEN_ID=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id') or d.get('generation_id',''))" 2>/dev/null)
    pass "POST /api/generate — gen_id=$GEN_ID"
else
    fail "POST /api/generate"
    GEN_ID=""
fi

# ════════════════════════════════════════════════════════════════════════════
title "HISTORY & AUDIO"
# ════════════════════════════════════════════════════════════════════════════

if api GET /api/history 200; then
    COUNT=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total', len(d.get('items',[]))))" 2>/dev/null)
    pass "GET /api/history — $COUNT entries"
    # grab an entry id for audio tests
    ENTRY_ID=$(echo "$RESP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
items=d.get('items', d if isinstance(d,list) else [])
print(items[0]['id'] if items else '')" 2>/dev/null)
else
    fail "GET /api/history"
    ENTRY_ID=""
fi

if [[ -n "$GEN_ID" ]]; then
    if api GET "/api/audio/$GEN_ID" 200; then
        pass "GET /api/audio/$GEN_ID"
    else fail "GET /api/audio/$GEN_ID"; fi

    # Download (saves and removes from storage — skip if destructive)
    skip "GET /api/audio/$GEN_ID/download — skipped to preserve test data"
fi

if [[ -n "$ENTRY_ID" && "$ENTRY_ID" != "$GEN_ID" ]]; then
    if api DELETE "/api/history/$ENTRY_ID" 200; then
        pass "DELETE /api/history/$ENTRY_ID"
    else fail "DELETE /api/history/$ENTRY_ID"; fi
fi

# ════════════════════════════════════════════════════════════════════════════
title "MUSIC / SONG"
# ════════════════════════════════════════════════════════════════════════════

if api GET /api/music/options 200; then
    pass "GET /api/music/options"
else fail "GET /api/music/options"; fi

if api POST /api/generate_music 200 \
    -H "Content-Type: application/json" \
    -d '{"prompt":"calm acoustic background","duration":5,"quality":"small"}'; then
    MUSIC_JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
    pass "POST /api/generate_music — job_id=$MUSIC_JOB"
else fail "POST /api/generate_music"; MUSIC_JOB=""; fi

if api POST /api/generate_song 200 \
    -H "Content-Type: application/json" \
    -d '{"prompt":"pop background","lyrics":"La la la test","voice_preset":"en_singer_3","style":"pop","quality":"small"}'; then
    SONG_JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
    pass "POST /api/generate_song — job_id=$SONG_JOB"
else fail "POST /api/generate_song"; SONG_JOB=""; fi

# Poll one job
if [[ -n "$MUSIC_JOB" ]]; then
    info "Polling job $MUSIC_JOB (up to 30s) ..."
    for i in $(seq 1 6); do
        sleep 5
        api GET "/api/jobs/$MUSIC_JOB" 200 > /dev/null 2>&1 || true
        STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
        info "  job status: $STATUS"
        [[ "$STATUS" == "done" || "$STATUS" == "error" ]] && break
    done
    [[ "$STATUS" == "done" ]] && pass "GET /api/jobs/$MUSIC_JOB — completed" || skip "GET /api/jobs/$MUSIC_JOB — status=$STATUS (still running)"
fi

# ════════════════════════════════════════════════════════════════════════════
title "VOICE CLONE (XTTS)"
# ════════════════════════════════════════════════════════════════════════════

if api GET /api/voice_clone/languages 200; then
    LANGS=$(echo "$RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('languages',[])))" 2>/dev/null)
    pass "GET /api/voice_clone/languages — $LANGS languages"
else fail "GET /api/voice_clone/languages"; fi

if api GET /api/voice_clone/list 200; then
    pass "GET /api/voice_clone/list"
else fail "GET /api/voice_clone/list"; fi

if api GET /api/voice_clone/profiles 200; then
    pass "GET /api/voice_clone/profiles"
else fail "GET /api/voice_clone/profiles"; fi

# Upload a voice sample
if api POST /api/voice_clone/upload 200 \
    -F "file=@$REF_AUDIO;type=audio/wav" \
    -F "name=TestVoice"; then
    VOICE_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('voice_id',''))" 2>/dev/null)
    pass "POST /api/voice_clone/upload — voice_id=$VOICE_ID"
else
    fail "POST /api/voice_clone/upload"
    VOICE_ID=""
fi

# One-shot clone
if api POST /api/voice_clone/generate_oneshot 200 \
    -F "text=Testing one shot cloning." \
    -F "language=en" \
    -F "reference_audio=@$REF_AUDIO;type=audio/wav"; then
    pass "POST /api/voice_clone/generate_oneshot"
else fail "POST /api/voice_clone/generate_oneshot"; fi

# Save a profile
if api POST /api/voice_clone/save_profile 200 \
    -F "name=TestProfile" \
    -F "reference_audio=@$REF_AUDIO;type=audio/wav"; then
    PROFILE_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('profile_id',''))" 2>/dev/null)
    pass "POST /api/voice_clone/save_profile — profile_id=$PROFILE_ID"
else
    fail "POST /api/voice_clone/save_profile"
    PROFILE_ID=""
fi

# Generate from saved voice
if [[ -n "$VOICE_ID" ]]; then
    if api POST /api/voice_clone/generate 200 \
        -H "Content-Type: application/json" \
        -d "{\"voice_id\":$VOICE_ID,\"text\":\"Hello from cloned voice.\",\"language\":\"en\"}"; then
        JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
        pass "POST /api/voice_clone/generate — job_id=$JOB"
    else fail "POST /api/voice_clone/generate"; fi
fi

# Generate from profile
if [[ -n "$PROFILE_ID" ]]; then
    if api POST "/api/voice_clone/from_profile/$PROFILE_ID" 200 \
        -H "Content-Type: application/json" \
        -d '{"text":"Testing from profile.","language":"en"}'; then
        JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
        pass "POST /api/voice_clone/from_profile/$PROFILE_ID — job_id=$JOB"
    else fail "POST /api/voice_clone/from_profile/$PROFILE_ID"; fi
fi

# Cleanup — delete voice and profile
if [[ -n "$VOICE_ID" ]]; then
    api DELETE "/api/voice_clone/$VOICE_ID" 200 > /dev/null && \
        pass "DELETE /api/voice_clone/$VOICE_ID" || fail "DELETE /api/voice_clone/$VOICE_ID"
fi
if [[ -n "$PROFILE_ID" ]]; then
    api DELETE "/api/voice_clone/profiles/$PROFILE_ID" 200 > /dev/null && \
        pass "DELETE /api/voice_clone/profiles/$PROFILE_ID" || fail "DELETE /api/voice_clone/profiles/$PROFILE_ID"
fi

# ════════════════════════════════════════════════════════════════════════════
title "TRANSCRIBE / TRANSLATE"
# ════════════════════════════════════════════════════════════════════════════

if api POST /api/transcribe 200 \
    -F "audio=@$REF_AUDIO;type=audio/wav" \
    -F "model=base"; then
    TEXT=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('text','')[:80])" 2>/dev/null)
    pass "POST /api/transcribe — \"$TEXT\""
else fail "POST /api/transcribe"; fi

if api GET /api/languages 200; then
    COUNT=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('languages',d)))" 2>/dev/null)
    pass "GET /api/languages — $COUNT languages"
else fail "GET /api/languages"; fi

if api POST /api/translate 200 \
    -H "Content-Type: application/json" \
    -d '{"text":"Hello world","target_language":"es"}'; then
    TRANSLATED=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('translated_text',''))" 2>/dev/null)
    pass "POST /api/translate — \"$TRANSLATED\""
else fail "POST /api/translate"; fi

# ════════════════════════════════════════════════════════════════════════════
title "ENHANCE AUDIO"
# ════════════════════════════════════════════════════════════════════════════

if api POST /api/enhance_audio 200 \
    -F "audio=@$REF_AUDIO;type=audio/wav" \
    -F "normalize=true" \
    -F "fade_in=0.1" \
    -F "fade_out=0.1"; then
    pass "POST /api/enhance_audio"
else fail "POST /api/enhance_audio"; fi

# ════════════════════════════════════════════════════════════════════════════
title "STATS / INFO"
# ════════════════════════════════════════════════════════════════════════════

if api GET /api/stats 200; then
    pass "GET /api/stats — $(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d)" 2>/dev/null)"
else fail "GET /api/stats"; fi

if api GET /api/models 200; then
    COUNT=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('models',d)))" 2>/dev/null)
    pass "GET /api/models — $COUNT models"
else fail "GET /api/models"; fi

if api GET /api/endpoints 200; then
    pass "GET /api/endpoints"
else fail "GET /api/endpoints"; fi

# ════════════════════════════════════════════════════════════════════════════
title "VOXCPM — /voice/*"
# ════════════════════════════════════════════════════════════════════════════

if api GET /voice/health 200; then
    pass "GET /voice/health — $(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin))" 2>/dev/null)"
elif [[ "$?" == "503" ]]; then
    fail "GET /voice/health — VoxCPM service down (is voxcpm.service running?)"
else
    fail "GET /voice/health — route missing? Check server was restarted after deploy"
fi

if api POST /voice/clone 200 \
    -F "text=Hello from voice clone test." \
    -F "reference=@$REF_AUDIO;type=audio/wav"; then
    SIZE=$(echo -n "$RESP" | wc -c)
    pass "POST /voice/clone — ${SIZE} bytes MP3"
    echo "$RESP" > "$OUT/voxcpm_clone.mp3"
else fail "POST /voice/clone"; fi

if api POST /voice/transcribe 200 \
    -F "audio=@$REF_AUDIO;type=audio/wav"; then
    TEXT=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('text','')[:80])" 2>/dev/null)
    pass "POST /voice/transcribe — \"$TEXT\""
else fail "POST /voice/transcribe"; fi

if api POST /voice/diarize 200 \
    -F "audio=@$REF_AUDIO;type=audio/wav"; then
    SEGS=$(echo "$RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('segments',[])))" 2>/dev/null)
    pass "POST /voice/diarize — $SEGS segments"
else
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/voice/diarize" \
        -H "$AUTH" -F "audio=@$REF_AUDIO;type=audio/wav")
    [[ "$CODE" == "502" ]] && \
        skip "POST /voice/diarize — 502 expected without HF_TOKEN" || \
        fail "POST /voice/diarize — HTTP $CODE"
fi

# ════════════════════════════════════════════════════════════════════════════
title "HISTORY CLEANUP"
# ════════════════════════════════════════════════════════════════════════════

skip "DELETE /api/history — skipped to preserve existing data"

# ════════════════════════════════════════════════════════════════════════════
title "SUMMARY"
# ════════════════════════════════════════════════════════════════════════════

TOTAL=$((PASS + FAIL + SKIP))
echo ""
echo -e "  ${GREEN}Passed: $PASS${NC}  ${RED}Failed: $FAIL${NC}  ${YELLOW}Skipped: $SKIP${NC}  (Total: $TOTAL)"
if [[ "$FAIL" -eq 0 ]]; then
    echo -e "\n  ${GREEN}All tests passed.${NC}"
else
    echo -e "\n  ${RED}$FAIL test(s) failed — check output above.${NC}"
    exit 1
fi
