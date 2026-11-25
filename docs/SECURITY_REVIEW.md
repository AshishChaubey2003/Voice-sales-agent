# Security Review

> Voice Sales Agent — Application Security Review  
> Cloud verification: 2026-11-25  
> Scope: code review and static analysis (not a penetration test or formal audit)

## 1. Authentication

| Control | Status | Details |
|---|---|---|
| API key auth | ✅ Implemented | `api/deps.py:get_current_org` — SHA-256 hash lookup, never echoes the key |
| Missing key → 401 | ✅ Tested | `tests/test_auth.py:test_missing_api_key_is_rejected` |
| Invalid key → 401 | ✅ Tested | `tests/test_auth.py:test_invalid_api_key_is_rejected` |
| Key storage | ✅ Hashed | `api/auth.py:hash_api_key` — SHA-256; raw key never stored |
| Voice tokens | ✅ Short-lived HMAC | `voice/voice_auth.py` — signed JWT-style, 5 min TTL, min 32-char secret |
| Voice token secret validation | ✅ | Config validator rejects secrets < 32 chars |
| MCP auth | ✅ Per-tool key | Each MCP tool accepts `api_key` → same hash lookup |

**Finding: No rate limiting.** There is no rate-limiter on login/auth or any endpoint. For a portfolio demo this is acceptable; for production, add `slowapi` or a reverse-proxy rate limit.  
**Severity: Medium (production) / Low (demo)**

## 2. Authorization & Tenant Isolation

| Control | Status | Details |
|---|---|---|
| Org scoping on conversations | ✅ | Composite FK `(organization_id, conversation_id)` on all queries |
| Org scoping on leads | ✅ | Same composite FK; `UniqueConstraint(org_id, conv_id)` |
| Org scoping on knowledge | ✅ | `WHERE c.organization_id = :organization_id` in both vector and keyword SQL |
| Org scoping on handoffs | ✅ | `WHERE ... organization_id = org.id` on all handoff queries |
| Org scoping on sales reps | ✅ | `UniqueConstraint(id, organization_id)` + FK on handoff_requests |
| Cross-tenant → 404 (not 403) | ✅ Tested | `test_auth.py:test_org_cannot_read_another_orgs_conversation` |
| MCP cross-tenant blocked | ✅ Tested | `test_mcp.py:test_create_lead_cross_tenant_conversation` |

**No findings.** Tenant isolation is enforced at the database level through composite foreign keys and checked in every query path.

## 3. Consent Enforcement

See `docs/CONSENT_FLOW_VERIFICATION.md` for full analysis. Summary:

- Single gate function `check_lead_proposal()` called by all three paths (REST, LangGraph, MCP)
- Email must be read back by assistant, then visitor must say an affirmative "yes"
- `consent_prompt` and `consent_reply` are NOT NULL columns — a lead cannot exist without them
- **No bypass route exists** — the gate is in the data layer, not middleware

## 4. SQL Injection

| Surface | Status | Details |
|---|---|---|
| SQLAlchemy ORM queries | ✅ Safe | All model queries use `select()` with bound parameters |
| Raw text() queries (knowledge) | ✅ Safe | `api/knowledge.py` uses `:named` bind params in `text()` |
| No f-string SQL | ✅ Verified | `grep -rn 'f".*SELECT\|\.execute(f"' api/` returned zero results |

**No findings.**

## 5. Secrets Management

| Control | Status | Details |
|---|---|---|
| `.env` not in git | ✅ | `.gitignore` excludes `.env` (CI workflow also checks this) |
| `SecretStr` for all secrets | ✅ | `groq_api_key`, `postgres_password`, `redis_password`, `deepgram_api_key`, `voice_token_secret`, `langsmith_api_key`, `gemini_api_key`, `smtp_password` — all `SecretStr` |
| No secrets in logs | ✅ | Grep for `log.*api_key\|log.*password\|log.*secret` returned only SMTP login (legitimate) |
| `.env.example` has no values | ✅ | Placeholders only |
| CI uses placeholder keys | ✅ | `ci-placeholder-not-a-real-key` in workflow |

**No findings.**

## 6. Input Validation

| Surface | Status | Details |
|---|---|---|
| Pydantic models on all routes | ✅ | `ChatRequest`, `LeadInput`, `RepCreate`, handoff schemas |
| Email validation | ✅ | `EmailStr` via Pydantic (email-validator) |
| MCP inputs validated | ✅ | `api_key`, `query`, `top_k`, `email` all validated/clamped |
| `is_clear_yes()` length cap | ✅ | Rejects strings > 80 chars (DoS prevention on regex) |
| `search_knowledge_tool` query cap | ✅ | Truncated to 500 chars |

**No findings.**

## 7. Content Security Policy (CSP)

| Dashboard | CSP | Details |
|---|---|---|
| `/dashboard/evals` | ✅ Nonce-based | `script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'` |
| `/dashboard/sales` | ✅ Nonce-based | Same nonce-CSP pattern |
| Voice demo widget | ⚠️ No CSP | `voice/static/demo.html` served for local testing only |

**Finding: Demo widget has no CSP.** Acceptable for local-only testing page; production embed would need CSP headers.  
**Severity: Low**

## 8. CORS

No `CORSMiddleware` is configured. This means:
- **Same-origin requests only** by default (FastAPI/Starlette default)
- The widget embed docs should instruct deployers to add CORS for their domain
- This is **more secure than wildcard CORS** for a demo

**No findings for the current scope.** Deployment docs should cover CORS configuration.

## 9. MCP Tool Permissions

| Tool | Access | Consent Gate | Direct DB Access |
|---|---|---|---|
| `search_knowledge_tool` | Read-only | N/A | ❌ Uses `search_knowledge()` service |
| `get_sales_availability_tool` | Read-only | N/A | ❌ Uses `availability()` service |
| `create_lead_tool` | Write (leads) | ✅ Full consent gate | ❌ Uses `save_lead_from_tool()` |

**No raw SQL or table access** is exposed through MCP. All tools go through the same service functions the REST API uses.

## 10. Widget Security

| Control | Status | Details |
|---|---|---|
| No API key in browser JS | ✅ | Widget uses voice tokens, not org API keys |
| Token instructions visible | ✅ | Demo page says "Never paste an organization API key here" |
| No `innerHTML` usage | ✅ | Demo page uses `textContent` only |
| No `localStorage` for secrets | ✅ | Sales dashboard stores API key in JS variable only during session |

**No findings.**

## 11. Dependency Review

| Check | Method | Result |
|---|---|---|
| Known CVEs | `pip-audit` (CI job) | Added to CI workflow; advisory (continue-on-error) |
| Pinned voice stack | `constraints-ci.txt` | pipecat-ai==1.12.0, langgraph==1.2.24, mcp==2.3.0 |
| No `eval()`/`exec()` | Grep scan | Zero results in `api/` and `voice/` |

## 12. Sensitive Logging

| Check | Result |
|---|---|
| API keys in logs | None found |
| Passwords in logs | Only SMTP login (necessary, `get_secret_value()` call) |
| Stack traces to client | FastAPI default — 500s show "Internal Server Error", not traces |
| MCP errors | Wrapped in JSON `{error: "..."}`, no stack traces |

## Summary

| Category | Critical | High | Medium | Low |
|---|---|---|---|---|
| Authentication | 0 | 0 | 1 (rate limiting) | 0 |
| Authorization | 0 | 0 | 0 | 0 |
| Injection | 0 | 0 | 0 | 0 |
| Secrets | 0 | 0 | 0 | 0 |
| CSP | 0 | 0 | 0 | 1 (demo widget) |
| **Total** | **0** | **0** | **1** | **1** |

**Recommendations for production deployment:**
1. Add rate limiting (`slowapi` or reverse-proxy level)
2. Add CORS configuration documentation for deployers
3. Add CSP to the voice widget in production
4. Consider adding request logging/audit trail for lead creation events
5. Set up automated `pip-audit` alerts (GitHub Dependabot or the CI security job)

This review is a code-level analysis, not a penetration test.
