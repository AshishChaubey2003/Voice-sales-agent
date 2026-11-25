# Demo Script

> Voice Sales Agent — 3–5 Minute Reproducible Demonstration

## Setup (Before Demo)

1. API server running: `uvicorn api.main:app --port 8000`
2. Database migrated: `alembic upgrade head`
3. Knowledge ingested: `python scripts/ingest_knowledge.py`
4. Test org created with API key (or use existing)

## Demo Flow

### Act 1: Text Chat — Product Knowledge (60s)

```bash
# Create a conversation
CONV=$(curl -s -X POST http://localhost:8000/v1/conversations \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" | jq -r .conversation_id)

# Ask about pricing
curl -s -X POST "http://localhost:8000/v1/conversations/$CONV/messages" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "What plans does NimbusCRM offer and how much do they cost?"}' | jq .reply
```

**Show**: RAG retrieves real knowledge, response includes plan details.

### Act 2: Consent Flow — Lead Saved (90s)

```bash
# Visitor wants a demo
curl -s -X POST "http://localhost:8000/v1/conversations/$CONV/messages" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "I would like to schedule a demo. My name is Alice and my email is alice@example.com"}' | jq .reply

# AI reads back details and asks for consent — say yes
curl -s -X POST "http://localhost:8000/v1/conversations/$CONV/messages" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Yes, that is correct"}' | jq .reply
```

**Show**: Lead saved response. Verify in DB:
```sql
SELECT name, email, consent_reply FROM leads
WHERE conversation_id = '<conv-id>';
```

### Act 3: Consent Denied — No Lead (60s)

```bash
# New conversation
CONV2=$(curl -s -X POST http://localhost:8000/v1/conversations \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" | jq -r .conversation_id)

# Same flow but say no
curl -s -X POST "http://localhost:8000/v1/conversations/$CONV2/messages" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "I want a demo. Name: Bob, email: bob@example.com"}' | jq .reply

curl -s -X POST "http://localhost:8000/v1/conversations/$CONV2/messages" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "No, I changed my mind"}' | jq .reply
```

**Show**: No lead in DB for this conversation.
```sql
SELECT count(*) FROM leads WHERE conversation_id = '<conv2-id>';
-- Returns 0
```

### Act 4: Human Handoff (60s)

```bash
# Register a sales rep
curl -s -X POST http://localhost:8000/v1/sales/reps \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Jane Sales", "email": "jane@example.com"}' | jq .

# Set rep to available
REP_ID=$(curl -s http://localhost:8000/v1/sales/reps \
  -H "Authorization: Bearer $API_KEY" | jq -r '.[0].id')

curl -s -X PATCH "http://localhost:8000/v1/sales/reps/$REP_ID/status" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "available"}' | jq .status

# Check availability
curl -s http://localhost:8000/v1/sales/availability \
  -H "Authorization: Bearer $API_KEY" | jq .
```

**Show**: `available: true, available_count: 1`

### Act 5: Evaluation Dashboard (30s)

Open in browser: `http://localhost:8000/dashboard/evals`

**Show**: Live evaluation metrics, latency charts.

Open sales dashboard: `http://localhost:8000/dashboard/sales`

**Show**: Rep status, handoff queue.

### Act 6: Test Suite (30s)

```bash
python -m pytest -q --tb=line 2>&1 | tail -5
```

**Show**: 844+ passed, 0 failed.

## If Recording

Use these exact commands in sequence. Each curl command and its output should be visible.
The database queries prove consent enforcement is real, not faked.

## Voice Demo (Optional — Requires Hardware)

If microphone/speakers available, follow `docs/VOICE_TESTING.md` to show a live voice conversation.
