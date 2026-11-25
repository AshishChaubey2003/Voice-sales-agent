# Voice Testing Guide

> Voice Sales Agent — Testing the Real-Time Voice Pipeline

## Prerequisites

| Requirement | Details |
|---|---|
| Groq API key | With STT + TTS access |
| VOICE_TOKEN_SECRET | ≥32 character secret in `.env` |
| Microphone + speakers | Physical audio hardware |
| Chrome/Edge | WebRTC-capable browser |
| Running API server | `uvicorn api.main:app --port 8000` |

## Configuration

In `.env`:
```
GROQ_API_KEY=your-real-groq-key
GROQ_MODEL=llama-3.3-70b-versatile
TTS_PROVIDER=groq
VOICE_TOKEN_SECRET=your-secret-at-least-32-characters-long
```

## Step-by-Step Test

### 1. Start the API server
```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### 2. Mint a voice token
```bash
curl -s -X POST http://localhost:8000/v1/voice/tokens \
  -H "Authorization: Bearer YOUR_ORG_API_KEY" | jq .
```
Copy the `token` value.

### 3. Open the demo page
Navigate to: `http://localhost:8000/voice/static/demo.html`

### 4. Connect
1. Paste the voice token into the text area
2. Click **Connect**
3. Wait for status to show "Connected"
4. Speak into your microphone

### 5. Test scenarios

| Scenario | What to say | Expected |
|---|---|---|
| Product question | "What plans does NimbusCRM offer?" | AI answers from knowledge base |
| Consent flow | "I'd like a demo" → provide name/email → "yes" | Lead captured |
| Consent denial | "I'd like a demo" → provide name/email → "no" | Lead NOT saved |
| Handoff request | "Can I talk to someone?" | Handoff initiated if reps available |
| Interruption | Start speaking while AI is talking | AI stops and listens |

### 6. Verify in database
```sql
-- Check lead was saved
SELECT name, email, consent_reply FROM leads
WHERE organization_id = 'your-org-id'
ORDER BY created_at DESC LIMIT 1;
```

## Automated Tests (No Hardware)

The test suite includes voice pipeline contract tests that verify:
- Pipeline construction
- Token validation
- WebRTC session lifecycle
- Audio frame processing

```bash
python -m pytest tests/test_voice_runner_contract.py -q
```

These tests use mocked audio and do not require hardware.

## Blockers for Real Voice Testing

If voice testing cannot be completed, the exact blocker is one of:
1. **No Groq API key** — STT/TTS require a real key
2. **No VOICE_TOKEN_SECRET** — Token minting is disabled without it
3. **No audio hardware** — Cloud/CI environments lack microphones
4. **Network restrictions** — WebRTC requires UDP connectivity

Document the specific blocker rather than claiming voice was tested.
