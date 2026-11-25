# API Reference

> Voice Sales Agent — REST API & MCP Tools

All endpoints require `Authorization: Bearer <org-api-key>` or `X-API-Key: <key>` unless noted.

## Health

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/healthz` | None | Liveness check |
| GET | `/readyz` | None | Readiness check (Postgres + Redis) |

## Chat

| Method | Path | Description |
|---|---|---|
| POST | `/v1/conversations` | Create a new conversation |
| POST | `/v1/conversations/{id}/messages` | Send a message, get AI reply |

**POST /v1/conversations** → `201 Created`
```json
{"conversation_id": "uuid", "agent_id": "uuid"}
```

**POST /v1/conversations/{id}/messages**
```json
// Request
{"message": "Tell me about the Pro plan"}
// Response
{"reply": "The Pro plan includes...", "sources": [...], "llm_latency_ms": 450, "rag_latency_ms": 120, "handoff_status": null}
```

## Voice Tokens

| Method | Path | Description |
|---|---|---|
| POST | `/v1/voice/tokens` | Mint a short-lived voice session token |

Returns `{token, token_type, expires_in}`. The token is passed to the browser widget.
Requires `VOICE_TOKEN_SECRET` to be configured.

## Sales / Handoff

| Method | Path | Description |
|---|---|---|
| GET | `/v1/sales/availability` | Rep availability counts |
| POST | `/v1/sales/reps` | Register a sales rep |
| GET | `/v1/sales/reps` | List reps for the org |
| PATCH | `/v1/sales/reps/{id}/status` | Update rep status (available/busy/offline) |
| GET | `/v1/sales/handoffs` | List handoff requests |
| GET | `/v1/sales/handoffs/{id}` | Handoff detail with transcript |
| POST | `/v1/sales/handoffs/{id}/accept` | Rep claims a handoff (atomic) |
| POST | `/v1/sales/handoffs/{id}/messages` | Rep sends a message |
| POST | `/v1/sales/handoffs/{id}/resolve` | Rep resolves the handoff |
| POST | `/v1/conversations/{id}/handoff` | Request handoff for a conversation |
| GET | `/v1/conversations/{id}/messages` | Get conversation messages (with `?after=` cursor) |

## Evaluation

| Method | Path | Description |
|---|---|---|
| GET | `/v1/evals/runs` | List evaluation runs |
| GET | `/v1/evals/runs/{id}` | Run detail |
| GET | `/v1/evals/runs/{id}/cases` | Cases for a run |
| GET | `/v1/evals/runs/{id}/metrics` | Metrics for a run |
| GET | `/v1/evals/runs/{id}/compare` | Compare two runs |
| GET | `/v1/evals/registry` | Registered evaluation datasets |
| GET | `/v1/evals/monitoring` | Monitoring metrics |
| GET | `/v1/evals/baselines` | Baseline scores |
| GET | `/v1/evals/voice-latency` | Voice latency metrics |
| GET | `/v1/evals/live/summary` | Live evaluation summary |
| GET | `/v1/evals/live/latency` | Live latency percentiles |
| GET | `/v1/evals/live/turns` | Recent evaluated turns |
| POST | `/v1/evals/live/refresh` | Trigger live evaluation refresh |

## Dashboards (HTML)

| Path | Description |
|---|---|
| `/dashboard/evals` | Evaluation metrics dashboard (nonce-CSP) |
| `/dashboard/sales` | Sales availability + handoff queue (nonce-CSP) |

## MCP Tools (Streamable HTTP at `/mcp`)

| Tool | Description | Auth |
|---|---|---|
| `search_knowledge_tool` | RAG search over product knowledge | API key in `api_key` param |
| `get_sales_availability_tool` | Rep availability counts | API key in `api_key` param |
| `create_lead_tool` | Consent-gated lead creation | API key + conversation history |

MCP can also be used via stdio: `python -m api.mcp_server`

### create_lead_tool Parameters

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | Yes | Organization API key |
| `name` | string | Yes | Visitor's full name |
| `email` | string | Yes | Visitor's email |
| `conversation_id` | string (UUID) | Yes | Conversation from the API |
| `history` | list[{role, content}] | Yes | Recent turns ending with consent |
| `company` | string | No | Company name |
| `team_size` | string | No | Team size |
| `need` | string | No | What they need |

The consent gate validates that the assistant read the email back and the visitor said yes.
