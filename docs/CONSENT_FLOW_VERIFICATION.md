# Consent Flow Verification

> Voice Sales Agent — Consent-Based Lead Capture  
> Cloud verification: 2026-11-25 | 844 passed, 4 skipped, 0 failed

## 1. Architecture

The consent gate is a **single function**, `check_lead_proposal()` in `api/leads.py`, called by
every code path that attempts to save a lead:

| Entry Point | File | Calls |
|---|---|---|
| REST `/v1/chat` (LangGraph `consent_gate` node) | `api/chat_graph.py:consent_gate()` | `check_lead_proposal()` → `save_lead_from_tool()` |
| REST `/v1/chat` (legacy path) | `api/chat.py` | `save_lead_from_tool()` (internally calls `check_lead_proposal()`) |
| MCP `create_lead` tool | `api/mcp_server.py:create_lead_tool()` | `check_lead_proposal()` → `save_lead_from_tool()` |

### Gate logic (api/leads.py)

```
check_lead_proposal(history, arguments)
  ├── parse_lead_arguments(arguments)         # JSON → LeadInput (Pydantic: name + EmailStr)
  │     └── Invalid/malformed → (None, reason)
  └── consent_problem(history, email)
        ├── < 2 turns or last turn not user   → "Read back and ask permission"
        ├── Second-to-last not assistant       → "Read back and ask"
        ├── Email NOT in assistant's read-back → "Read back and ask"  (LEAD-01)
        └── is_clear_yes(last user message)
              ├── False → "Not clearly agreed"
              └── True  → None (consent valid)
```

`is_clear_yes()` rejects empty strings, strings >80 chars, messages containing negative words
(no, not, don't, nope, nah, never, stop, cancel, refuse, decline), and messages not starting
with an affirmative pattern (yes, yeah, sure, ok, correct, confirmed, go ahead, etc.).

## 2. Required Scenarios and Test Evidence

### Scenario 1: Consent YES saves a lead

**Code path**: `is_clear_yes("yes") → True`, `consent_problem → None`, `save_lead_from_tool → {status: "saved"}`

| Test | File | Evidence |
|---|---|---|
| `test_clear_yes_is_accepted` | tests/test_leads.py:19 | Parametrized: "yes", "Yes!", "yeah sure", "Sure, go ahead" etc. |
| `test_consent_passes_after_read_back_and_clear_yes` | tests/test_leads.py:40 | Full history → `check_lead_proposal` returns `(lead, None)` |
| `test_lead_is_saved_after_details_are_read_back_and_visitor_says_yes` | tests/test_chat.py:198 | End-to-end REST: DB row created, 200 response |
| `test_route_consented_lead_is_saved_through_the_graph_and_a_refusal_is_not` | tests/test_chat_graph.py:364 | LangGraph path: lead persisted in DB |
| `test_create_lead_saves_successfully` | tests/test_mcp.py:342 | MCP path: `{status: "saved"}` |

### Scenario 2: Consent NO does not save a lead

**Code path**: `is_clear_yes("no thanks") → False` (NEGATIVE_WORDS match), `consent_problem → "Not clearly agreed"`

| Test | File | Evidence |
|---|---|---|
| `test_unclear_or_negative_reply_is_not_consent` | tests/test_leads.py:26 | Parametrized: "no", "nah", "not now", "I don't think so", etc. |
| `test_lead_is_not_saved_without_read_back_and_confirmation` | tests/test_chat.py:224 | REST: LLM never receives save_lead tool, no DB row |
| `test_create_lead_consent_no` | tests/test_mcp.py:249 | MCP: `{status: "rejected"}` |
| `test_route_consented_lead_is_saved_through_the_graph_and_a_refusal_is_not` | tests/test_chat_graph.py:364 | Graph: refused case checked in same test |

### Scenario 3: Missing consent does not save a lead

**Code path**: History has <2 turns or user turn is not last → `consent_problem` returns rejection reason.

| Test | File | Evidence |
|---|---|---|
| `test_consent_requires_the_email_to_be_read_back_before_yes` | tests/test_leads.py:30 | History without read-back → `problem is not None` |
| `test_create_lead_email_not_read_back` | tests/test_mcp.py:270 | MCP: assistant did not echo email → `{status: "rejected"}` |
| `test_create_lead_no_consent` | tests/test_mcp.py:228 | MCP: too few turns → `{status: "rejected"}` |
| `test_lead_capture_still_enforces_consent` | tests/test_auth.py:222 | REST tenant-aware: consent enforced even with valid auth |
| `test_rejected_lead_goes_through_the_gate_but_never_reaches_capture` | tests/test_chat_graph.py:205 | Graph: consent gate blocks; DB row count unchanged |

### Scenario 4: Invalid data does not create a partial lead

**Code path**: `parse_lead_arguments` → Pydantic `ValidationError` on `LeadInput` → `(None, reason)`

| Test | File | Evidence |
|---|---|---|
| `test_invalid_email_is_rejected` | tests/test_leads.py:69 | "not-an-email" → Pydantic rejects, `problem is not None` |
| `test_create_lead_invalid_email` | tests/test_mcp.py:291 | MCP: `{status: "rejected"}` |
| `test_correction_rejects_a_save_proposing_the_superseded_email` | tests/test_leads.py:87 | Email mismatch with read-back (LEAD-01) |

### Scenario 5: Duplicate requests behave correctly

**Code path**: `save_lead_from_tool` → SELECT existing lead by (org_id, conv_id) → if exists, UPDATE instead of INSERT. Race condition handled via IntegrityError catch + rollback + re-select + update.

| Test | File | Evidence |
|---|---|---|
| `test_concurrent_lead_save_is_idempotent_not_an_unhandled_error` | tests/test_leads.py:158 | Simulates concurrent insert; both return `{status: "saved"}` |
| `test_create_lead_duplicate_idempotent` | tests/test_mcp.py:362 | MCP: two sequential saves → both `{status: "saved"}` |

### Scenario 6: Tenant isolation — one tenant cannot access another's leads

**Code path**: `organization_id` is resolved from the API key (SHA-256 hash lookup), then scoped to all queries via `WHERE organization_id = :org_id`. Composite foreign keys enforce this at the DB level.

| Test | File | Evidence |
|---|---|---|
| `test_org_cannot_read_another_orgs_conversation` | tests/test_auth.py:178 | REST: 404 for cross-tenant read |
| `test_org_cannot_write_to_another_orgs_conversation` | tests/test_auth.py:197 | REST: 404 for cross-tenant write |
| `test_create_lead_cross_tenant_conversation` | tests/test_mcp.py:325 | MCP: Org B key + Org A conv → `{status: "rejected"}` |
| `test_search_knowledge_tenant_isolation` | tests/test_mcp.py | Each org's search scoped to its own org_id |
| `test_get_availability_tenant_isolation` | tests/test_mcp.py | Each org's availability scoped to its own org_id |

## 3. Database Evidence (Cloud)

All tests run against a disposable PostgreSQL 16 + pgvector 0.8.0 database (`vsa_test`).
Alembic migration chain verified: fresh DB → `alembic upgrade head` → `alembic check` reports
"No new upgrade operations detected." Rollback tested: `alembic downgrade -1` for each migration.

**Key schema constraints enforcing consent**:
- `leads` table: `consent_prompt TEXT NOT NULL`, `consent_reply TEXT NOT NULL` — a lead cannot exist without recorded consent.
- `UniqueConstraint("organization_id", "conversation_id")` on `leads` — one lead per conversation per org.
- Composite FK `(organization_id, conversation_id) → conversations(organization_id, id)` — a lead cannot reference a conversation from another tenant.

## 4. Cloud vs Windows Status

| Item | Cloud | Windows |
|---|---|---|
| Consent gate unit tests (test_leads.py) | ✅ All passed | ⏳ Pending your pytest run |
| REST integration (test_chat.py, test_auth.py) | ✅ All passed | ⏳ Pending |
| LangGraph path (test_chat_graph.py) | ✅ All passed | ⏳ Pending |
| MCP path (test_mcp.py) | ✅ 17 passed, 3 skipped | ⏳ Pending |
| Full suite | ✅ 844 passed, 4 skipped | ⏳ Pending |

## 5. Conclusion

All six required consent scenarios are covered by **automated tests** that run against a real
PostgreSQL database. The consent gate (`check_lead_proposal`) is a single pure function called
by every lead-creation path (REST, LangGraph, MCP), so there is no bypass route. The gate
enforces: (1) valid JSON with a valid email, (2) the assistant read the email back in its
second-to-last turn, (3) the visitor's last message is an affirmative "yes." Any failure
returns `{status: "rejected", reason: "..."}` and **no database row is created or modified**.
