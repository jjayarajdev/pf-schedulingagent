# AI Outbound Calling — Phase 1: Scheduling Calls

## Context

PF needs AI-powered outbound scheduling calls. When a project status changes to "Ready for Auto Call", PF's rule engine validates eligibility (schedulable online, delivery/pre-install done, opt-out flags) and drops a message on SQS. Our bot consumes the message, authenticates the customer, fetches project details, and initiates an outbound call via Vapi. The AI guides the customer through appointment booking using the existing scheduling tools.

Reminders and surveys are deferred — Phase 1 is scheduling only.

## Architecture

```
PF Rule Engine                 SQS Queue                    Scheduling Bot                    Vapi
    │                              │                              │                              │
    │  Status → "Ready for        │                              │                              │
    │   Auto Call"                 │                              │                              │
    │  Pre-checks pass             │                              │                              │
    │──── SendMessage ────────────>│                              │                              │
    │                              │                              │                              │
    │                              │  ReceiveMessage (long poll)  │                              │
    │                              │<─────────────────────────────│                              │
    │                              │                              │                              │
    │                              │  {phone, client_id, ...}     │                              │
    │                              │─────────────────────────────>│                              │
    │                              │                              │  1. phone-call-login         │
    │                              │                              │  2. Create call record (DDB) │
    │                              │                              │  3. POST /call (Vapi API)    │
    │                              │                              │─────────────────────────────>│
    │                              │                              │                    Dials customer
    │                              │                              │                              │
    │                              │                              │  assistant-request           │
    │                              │                              │  (call.type=outboundPhoneCall)
    │                              │                              │<─────────────────────────────│
    │                              │                              │  Return outbound config      │
    │                              │                              │─────────────────────────────>│
    │                              │                              │                   AI speaks greeting
    │                              │                              │  tool-calls (scheduling)     │
    │                              │                              │<────────────────────────────>│
    │                              │                              │                              │
    │                              │                              │  end-of-call-report          │
    │                              │                              │<─────────────────────────────│
    │                              │                              │  Update call record          │
    │                              │                              │  Post notes to PF API        │
    │                              │  DeleteMessage               │                              │
    │                              │<─────────────────────────────│                              │
```

## Key Design Decisions

1. **SQS consumer in ECS bot** — Background asyncio task long-polls the queue (20s). No Lambda, no separate service. Keeps infrastructure simple and everything in one process.

2. **`serverUrl` mode** — Outbound calls use `serverUrl` (our webhook) so Vapi sends `assistant-request` back to us. We return dynamic outbound configs, reusing the same webhook pipeline as inbound.

3. **Auth via phone-call-login** — We authenticate the customer using their phone number (same as inbound). SQS message has basic info; we fetch full context after auth.

4. **In-memory active calls cache** — During an active outbound call, we cache the call context in memory (keyed by `vapi_call_id`). Avoids DynamoDB read on every `tool-calls` event. Cleared on end-of-call.

5. **Scheduling tools reused as-is** — The outbound call flow maps to existing tools (`get_available_dates`, `get_time_slots`, `confirm_appointment`, `add_note`, `get_installation_address`). Only the Vapi-level system prompt differs.

6. **PF owns all trigger logic** — Status changes, eligibility checks, opt-out flags — all PF. We just consume and call.

7. **Address corrections as notes** — No address update API. Corrections captured via `add_note` with "ADDRESS CORRECTION:" prefix.

---

## SQS Infrastructure

### Queue Setup

| Setting | Value |
|---------|-------|
| Queue name | `pf-syn-schedulingagents-outbound-queue-{env}` |
| DLQ name | `pf-syn-schedulingagents-outbound-dlq-{env}` |
| Visibility timeout | 120s (auth + Vapi call initiation) |
| Message retention | 4 days |
| DLQ max receives | 3 (→ DLQ after 3 failed attempts) |
| Long poll wait | 20 seconds |

### Expected SQS Message Schema (from PF)

```json
{
  "project_id": "90000119",
  "client_id": "synrg",
  "customer_phone": "+15551234567",
  "customer_phone_alt": "+15559876543",
  "customer_name": "Sarah Johnson",
  "customer_id": "181",
  "project_type": "Windows Installation"
}
```

Lightweight — we do the heavy lifting after consuming: phone-call-login → get project details → get address → initiate call.

### Provisioning Script

**File**: `env_setup/08-sqs.sh` (CREATE)

Creates queue + DLQ with redrive policy. Adds SQS permissions to the ECS task role.

---

## Implementation Steps

### Step 1: Config + DynamoDB Table

**Files**: `src/config.py`, `env_setup/01-dynamodb.sh`

Add to `config.py`:
```python
outbound_calls_table: str = ""       # DynamoDB table for outbound call records
outbound_queue_url: str = ""         # SQS queue URL
```

With auto-derivation:
```python
if not self.outbound_calls_table:
    self.outbound_calls_table = f"pf-syn-schedulingagents-outbound-calls-{env}"
if not self.outbound_queue_url:
    self.outbound_queue_url = ""  # Must be set via env var (contains account ID)
```

**DynamoDB table**: `pf-syn-schedulingagents-outbound-calls-{env}`

```
PK: call_id (S)                  — UUID assigned at consume time
GSI: project-calls-index
  PK: project_id (S)
  SK: created_at (S)
TTL: ttl (N)                     — 30-day retention
```

**Item schema**:

| Field | Type | Description |
|-------|------|-------------|
| `call_id` | S | Our internal UUID |
| `project_id` | S | PF project ID |
| `customer_id` | S | PF customer ID |
| `client_id` | S | Tenant ID |
| `call_type` | S | `scheduling` (Phase 1 only) |
| `status` | S | `pending` / `calling` / `in_progress` / `completed` / `voicemail` / `no_answer` / `failed` / `callback_requested` |
| `attempt_number` | N | Current attempt (1, 2) |
| `max_attempts` | N | Default 2 (primary + alternate) |
| `phone_primary` | S | Customer primary phone |
| `phone_alternate` | S | Customer alternate phone |
| `phone_used` | S | Phone used for this attempt |
| `vapi_call_id` | S | Vapi's call ID (set after API call) |
| `vapi_phone_number_id` | S | Vapi phone number used for outbound |
| `customer_name` | S | Full name |
| `client_name` | S | Tenant name |
| `project_type` | S | Job category (e.g., "Windows Installation") |
| `installation_address` | M | `{address1, city, state, zipcode}` |
| `auth_creds` | M | Cached auth (bearer_token, client_id, customer_id) |
| `call_result` | M | Outcome after call ends |
| `sqs_message_id` | S | SQS message ID (for tracking) |
| `sqs_receipt_handle` | S | SQS receipt handle (for deletion on success) |
| `created_at` | S | ISO timestamp |
| `updated_at` | S | ISO timestamp |
| `ttl` | N | 30-day epoch |

- [ ] Add `outbound_calls_table` and `outbound_queue_url` to `Settings` in `config.py`
- [ ] Add table 5 (outbound calls + GSI) to `env_setup/01-dynamodb.sh`
- [ ] Run script on dev + qa

### Step 2: SQS Queue Provisioning

**File**: `env_setup/08-sqs.sh` (CREATE)

- [ ] Create DLQ: `pf-syn-schedulingagents-outbound-dlq-{env}`
- [ ] Create queue: `pf-syn-schedulingagents-outbound-queue-{env}` with redrive policy pointing to DLQ
- [ ] Update IAM task role policy to add SQS permissions (`sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`) on the queue ARN
- [ ] Add outbound-calls DynamoDB table ARN to the existing DynamoDB IAM statement
- [ ] Run script on dev + qa

### Step 3: Outbound Call Store

**File**: `src/channels/outbound_store.py` (CREATE)

```python
# DynamoDB CRUD
async def create_outbound_call(call_data: dict) -> str
async def get_outbound_call(call_id: str) -> dict | None
async def update_outbound_call(call_id: str, updates: dict) -> None
async def get_calls_for_project(project_id: str) -> list[dict]

# In-memory cache for active calls (avoids DDB read on every tool-call event)
_active_calls: dict[str, dict] = {}   # vapi_call_id → call_data
def cache_active_call(vapi_call_id: str, call_data: dict) -> None
def get_active_call(vapi_call_id: str) -> dict | None
def remove_active_call(vapi_call_id: str) -> None
```

Follows patterns from `conversation_log.py`.

- [ ] Create `src/channels/outbound_store.py`
- [ ] Write tests `tests/unit/test_outbound_store.py` (DynamoDB CRUD + cache)

### Step 4: Vapi Outbound API Client

**File**: `src/channels/outbound_vapi.py` (CREATE)

```python
async def create_vapi_call(
    phone_number_id: str,
    customer_phone: str,
    customer_name: str,
    server_url: str,
    metadata: dict | None = None,
) -> dict:
    """POST https://api.vapi.ai/call — initiate outbound call.

    Uses Bearer {vapi_api_key} from SecretsCache.
    Returns Vapi response with call ID.
    Passes serverUrl so Vapi sends assistant-request back to our webhook.
    """

async def get_vapi_call_status(vapi_call_id: str) -> dict:
    """GET https://api.vapi.ai/call/{id} — check call status."""
```

Uses `httpx.AsyncClient`, `log_curl()`, `log_response()` patterns.

- [ ] Create `src/channels/outbound_vapi.py`
- [ ] Write tests `tests/unit/test_outbound_vapi.py`

### Step 5: SQS Consumer

**File**: `src/channels/outbound_consumer.py` (CREATE)

Background asyncio task that long-polls SQS and processes outbound call requests.

```python
async def start_outbound_consumer() -> None:
    """Start the SQS consumer loop. Called from main.py on startup."""
    # Runs forever in background, long-polls SQS queue
    # On each message: parse → authenticate → create call record → initiate Vapi call

async def _process_outbound_message(message: dict) -> None:
    """Process a single SQS message.

    1. Parse message body (project_id, client_id, customer_phone, etc.)
    2. Call phone-call-login to authenticate → get bearer_token, customer_id
    3. Get project details (list_projects or get_project_details) for project type, address
    4. Create outbound call record in DynamoDB (status=pending)
    5. Call create_vapi_call() with serverUrl = our webhook
    6. Update record with vapi_call_id, status=calling
    7. Cache in _active_calls (keyed by vapi_call_id)
    8. Delete SQS message on success
    """

async def stop_outbound_consumer() -> None:
    """Graceful shutdown — stop polling, wait for in-flight processing."""
```

**Startup integration** (`src/main.py`):
```python
@app.on_event("startup")
async def startup():
    ...
    asyncio.create_task(start_outbound_consumer())

@app.on_event("shutdown")
async def shutdown():
    await stop_outbound_consumer()
```

- [ ] Create `src/channels/outbound_consumer.py`
- [ ] Add startup/shutdown hooks in `main.py`
- [ ] Write tests `tests/unit/test_outbound_consumer.py`

### Step 6: Status + Admin Endpoints

**File**: `src/channels/outbound.py` (CREATE)

```python
router = APIRouter(prefix="/outbound", tags=["Outbound Calls"])

@router.get("/{call_id}/status")
async def get_outbound_status(call_id: str) -> dict:
    """Check outbound call status. Used by PF to poll call outcome."""

@router.get("/calls")
async def list_outbound_calls(project_id: str = "") -> dict:
    """List outbound calls, optionally filtered by project_id."""

@router.post("/trigger")
async def manual_trigger(request: OutboundTriggerRequest) -> dict:
    """Manual trigger for testing. Bypasses SQS — directly initiates a call.
    Auth: x-vapi-secret header."""
```

The `/trigger` endpoint is for testing/dev. Production flow goes through SQS. Both paths converge to the same `_process_outbound_message` logic.

**Schemas** (`src/channels/schemas.py`):
```python
class OutboundTriggerRequest(BaseModel):
    project_id: str
    client_id: str
    customer_phone: str                # Primary (E.164)
    customer_phone_alt: str = ""       # Alternate
    customer_name: str = ""
    customer_id: str = ""
    project_type: str = ""
    vapi_phone_number_id: str = ""     # Vapi phone to call FROM (default: tenant number)
    metadata: dict = {}
```

- [ ] Create `src/channels/outbound.py` (status + trigger endpoints)
- [ ] Add `OutboundTriggerRequest` to `src/channels/schemas.py`
- [ ] Register `outbound_router` in `src/main.py`
- [ ] Write tests `tests/unit/test_outbound.py`

### Step 7: Outbound Detection in Vapi Webhook

**File**: `src/channels/vapi.py` — MODIFY

#### 7A. Detect outbound in assistant-request

Add at the top of `_handle_assistant_request()`:
```python
call_type = call_data.get("type", "")
our_call_id = call_data.get("metadata", {}).get("call_id", "")

if call_type == "outboundPhoneCall" and our_call_id:
    return await _handle_outbound_assistant_request(body, call_data, our_call_id)
```

#### 7B. Outbound assistant request handler

```python
async def _handle_outbound_assistant_request(
    body: dict, call_data: dict, our_call_id: str
) -> dict:
```

1. Look up call from `_active_calls` cache (or DynamoDB fallback)
2. Update status to `in_progress`
3. Generate outbound greeting (customer name + company + project type)
4. Build outbound scheduling config
5. Return `{"assistant": config}`

#### 7C. Outbound scheduling config

```python
def _build_outbound_scheduling_config(
    first_message: str, server_secret: str, outbound_call: dict,
    support_number: str, hours_context: dict
) -> dict:
```

Same structure as `_build_assistant_config()` but with outbound-specific system prompt:

**6-Step Call Flow:**
1. **Introduction** — `firstMessage` (SSML greeting: "Hello {name}! This is J from {company}. I'm calling about your {project_type} project. Is now a good time?")
2. **Availability check** — "Is now a good time?" → NO: offer callback, end gracefully (status=`callback_requested`). YES: proceed.
3. **Scheduling** — Use `ask_scheduling_bot` → get available dates → customer picks date → get time slots → picks time → confirm appointment
4. **Address confirmation** — Read address aloud. Corrections → `add_note` with "ADDRESS CORRECTION:" prefix
5. **Additional notes** — "Anything we should know before arrival?" → `add_note` with "CUSTOMER NOTE:" prefix
6. **Wrap-up** — Summarize date/time/address. "You'll get a confirmation text/email. Thank you!"

**Tools**: Same `ask_scheduling_bot` + `transferCall` as inbound.

#### 7D. Outbound auth context for tool calls

Modify `_set_auth_context_from_phone()` — at the top, before existing phone auth:
```python
vapi_call_id = call_data.get("id", "")
outbound = get_active_call(vapi_call_id)
if outbound and outbound.get("auth_creds"):
    AuthContext.set(
        auth_token=outbound["auth_creds"]["bearer_token"],
        client_id=outbound["auth_creds"]["client_id"],
        customer_id=outbound["auth_creds"]["customer_id"],
    )
    return
```

#### 7E. End-of-call for outbound

Extend `_handle_server_event()` — when `event_type == "end-of-call-report"`:
```python
outbound = get_active_call(call_id)
if outbound:
    outcome = _classify_outbound_outcome(ended_reason, summary)
    await update_outbound_call(outbound["call_id"], {
        "status": outcome["status"],
        "call_result": outcome,
    })
    remove_active_call(call_id)
    # Retry on alternate number if applicable
    if outcome["status"] in ("no_answer", "voicemail") and can_retry(outbound):
        asyncio.create_task(_retry_outbound_call(outbound))
```

#### 7F. Outbound greeting generator

```python
def _generate_outbound_greeting(
    customer_name: str, client_name: str, project_type: str
) -> str:
```

Uses SSML `<break>` pauses like `_generate_dynamic_greeting()`.

- [ ] Add outbound detection in `_handle_assistant_request()` (4 lines)
- [ ] Add `_handle_outbound_assistant_request()` function
- [ ] Add `_build_outbound_scheduling_config()` with 6-step prompt
- [ ] Modify `_set_auth_context_from_phone()` for cached outbound creds
- [ ] Extend end-of-call handler for outbound status tracking
- [ ] Add `_generate_outbound_greeting()` function
- [ ] Add outbound-specific tests to `tests/unit/test_vapi.py`

### Step 8: Retry Logic

**File**: `src/channels/outbound_consumer.py` — add retry function

```python
async def _retry_outbound_call(outbound_call: dict) -> None:
    """Retry on alternate phone number after no_answer/voicemail."""
```

When end-of-call reports `no_answer`, `voicemail`, or `busy`:
1. Check `attempt_number < max_attempts` and alternate phone exists
2. Update record with new attempt + alternate phone
3. Call Vapi API with alternate phone
4. If max attempts exhausted → final status = `no_answer` / `voicemail`

Voicemail message (on Vapi config):
```python
"voicemailDetectionType": "transcript",
"voicemailMessage": (
    f"Hello {customer_name}, this is J from {client_name}. "
    f"I'm calling about your {project_type} project. "
    "We'd like to schedule your installation at a convenient time. "
    f"Please call us back at {support_number}. Thank you!"
),
```

- [ ] Add retry logic to `outbound_consumer.py`
- [ ] Add voicemail config to `_build_outbound_scheduling_config()`
- [ ] Write retry tests

### Step 9: Tests

| File | What |
|------|------|
| `tests/unit/test_outbound_store.py` | CREATE — DynamoDB CRUD, active call cache |
| `tests/unit/test_outbound_vapi.py` | CREATE — Vapi API client: create call, auth, errors |
| `tests/unit/test_outbound_consumer.py` | CREATE — SQS consumer: parse, auth, initiate, retry |
| `tests/unit/test_outbound.py` | CREATE — Status endpoint, manual trigger |
| `tests/unit/test_vapi.py` | MODIFY — Add outbound assistant-request, auth context, end-of-call |

- [ ] Write all test files
- [ ] Ensure all existing tests still pass
- [ ] Run full test suite

### Step 10: Deploy + Verify

- [ ] Run SQS provisioning script on dev + qa
- [ ] Run DynamoDB table script on dev + qa
- [ ] Update IAM roles on dev + qa
- [ ] Deploy to dev
- [ ] Verify health check
- [ ] Test manual trigger: `curl -X POST /outbound/trigger` with test project
- [ ] Receive call on test phone → AI introduces itself → scheduling flow works → notes posted
- [ ] Check DynamoDB outbound-calls table for status tracking
- [ ] Check CloudWatch logs for outbound call lifecycle
- [ ] Test voicemail scenario
- [ ] Test alternate number retry
- [ ] Deploy to qa

---

## Files Summary

| File | Action | Step |
|------|--------|------|
| `src/config.py` | MODIFY | 1 |
| `env_setup/01-dynamodb.sh` | MODIFY | 1 |
| `env_setup/08-sqs.sh` | CREATE | 2 |
| `env_setup/02-iam-roles.sh` | MODIFY | 2 |
| `src/channels/outbound_store.py` | CREATE | 3 |
| `src/channels/outbound_vapi.py` | CREATE | 4 |
| `src/channels/outbound_consumer.py` | CREATE | 5 |
| `src/channels/outbound.py` | CREATE | 6 |
| `src/channels/schemas.py` | MODIFY | 6 |
| `src/main.py` | MODIFY | 5, 6 |
| `src/channels/vapi.py` | MODIFY | 7 |
| `tests/unit/test_outbound_store.py` | CREATE | 9 |
| `tests/unit/test_outbound_vapi.py` | CREATE | 9 |
| `tests/unit/test_outbound_consumer.py` | CREATE | 9 |
| `tests/unit/test_outbound.py` | CREATE | 9 |
| `tests/unit/test_vapi.py` | MODIFY | 9 |

---

## What PF Needs To Provide

| # | Item | Status |
|---|------|--------|
| 1 | SQS message schema (exact fields) | Pending — using assumed schema above |
| 2 | Vapi phone number ID for outbound (per tenant/env) | Pending |
| 3 | SQS queue ARN (after PF creates it, or we create it) | Pending — script ready |
| 4 | Test project in "Ready for Auto Call" status | Needed for E2E test |

## Open Questions

| # | Question | Impact |
|---|----------|--------|
| 1 | Who creates the SQS queue — us or PF? | Script ready either way |
| 2 | Does the SQS message include `vapi_phone_number_id`, or do we look it up from tenant config? | Determines if we need a tenant→phone mapping |
| 3 | Should we expose a cancel endpoint (`POST /outbound/{call_id}/cancel`) for in-flight calls? | PF may want to abort if customer calls in manually |
| 4 | Should "not now" responses also trigger an SMS? | BRD mentions "callback via text" |
| 5 | Max retry attempts — is 2 (primary + alternate) sufficient? | Configurable later if needed |
