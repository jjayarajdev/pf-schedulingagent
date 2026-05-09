# ProjectsForce Scheduling AI Bot

AI-powered scheduling assistant for ProjectsForce 360 field service management. Handles appointment scheduling, rescheduling, cancellation, weather-aware recommendations, project notes, and address updates across **four channels**: web chat, phone (Vapi), SMS (Pinpoint), and outbound calling (SQS-driven).

> **Current production version:** `v1.4.9` (2026-05-09) — see [Version History](#version-history) for what's in each release. Default branch: `release`.

---

## Table of contents

1. [Architecture](#architecture)
2. [Tech stack](#tech-stack)
3. [Quick start](#quick-start)
4. [Project structure](#project-structure)
5. [Scheduling tools](#scheduling-tools)
6. [Vapi phone channel](#vapi-phone-channel)
7. [Outbound calling](#outbound-calling)
8. [SMS channel](#sms-channel)
9. [API endpoints](#api-endpoints)
10. [DynamoDB tables](#dynamodb-tables)
11. [Branch model](#branch-model)
12. [Deployment](#deployment)
13. [Versioning + rollback](#versioning--rollback)
14. [Testing strategy](#testing-strategy)
15. [Environment](#environment)
16. [Version history](#version-history)
17. [Operations & troubleshooting](#operations--troubleshooting)

---

## Architecture

Four channels, one AgentSquad orchestrator, three agents, single AWS account split across two regions.

```
  WEB CHAT             PHONE (Vapi)           SMS (Pinpoint)       OUTBOUND (SQS)
  POST /chat           POST /vapi/webhook     POST /sms/webhook    SQS Consumer
       |                    |                       |                    |
       |                    | assistant-request      |                    |
       |                    | (dynamic greeting,     |                    |
       |                    |  phone auth,           |                    |
       |                    |  caller_type routing,  |                    |
       |                    |  office hours,         |                    |
       |                    |  blind transfer)       |                    |
       |                    |                       |                    |
       |               POST /vapi/chat/completions  |                    |
       |                    | (Custom LLM endpoint)  |                    |
       |                    |                       |                    |
       +--------------------+---+---------+---------+--------------------+
                                |         |
                                v         v
                        +---------------+
                        |  AgentSquad   |
                        |  Orchestrator |
                        +-------+-------+
              +-----------------+---+-------------+
              v                 v                 v
        Scheduling         Chitchat          Weather
         Agent              Agent             Agent
        (13 tools)        (no tools)        (1 tool)
              |                                 |
              v                                 v
        PF CX Portal                      Open-Meteo
           API                               API
```

| Agent | Purpose | Tools |
|-------|---------|-------|
| **Scheduling Agent** (default) | Project listing, scheduling, rescheduling, cancellation, notes, address, weather | 13 tools (PF CX Portal API + Open-Meteo) |
| **Chitchat Agent** | Greetings, help, small talk | None |
| **Weather Agent** | Weather forecasts for project locations | Open-Meteo API |

### Caller-type routing (post v1.4.7)

Inbound Vapi calls are routed by the `caller_type` field returned from `POST /authentication/phone-call-login`:

| `caller_type` | `auth_status` | Routed to |
|---|---|---|
| `user` | `success` | Custom-LLM customer flow (full scheduling) |
| `store` | `failed` | Store-caller flow (PO/project-number lookup, status only, no PII) |
| `unknown` | `not_found` | Lead-capture flow (Phase 3A) — **only if tenant has `lead_capture_enabled=true`**, else falls through to store flow |

---

## Tech stack

| Layer | Tech | Notes |
|---|---|---|
| Language | Python 3.12 | UV package manager |
| API framework | FastAPI on ECS Fargate (uvicorn) | Linux/amd64 Docker images |
| Orchestration | [AgentSquad](https://github.com/awslabs/agent-squad) (`agent-squad[aws]`) | Multi-agent routing + classifier |
| LLM (backend) | Anthropic Sonnet 4 on Amazon Bedrock | Via `agent-squad[aws]` BedrockLLMAgent |
| LLM (Vapi) | OpenAI GPT-5.2-chat-latest | Vapi-managed for assistant-side reasoning |
| Voice (TTS) | ElevenLabs `eleven_turbo_v2_5` (voice "Lauren B." `3liN8q8YoeB9Hk6AboKe`) | similarityBoost 0.75, stability 0.5 — see [SSML rules](#ssml-and-tts-do-nots) |
| Voice (STT) | Deepgram Nova-3 (endpointing 150ms) | Configured in Vapi assistant |
| Phone | Vapi.ai | Twilio + Vapi-managed numbers, SIP REFER blind transfer |
| SMS | AWS End User Messaging (`pinpoint-sms-voice-v2`) | Inbound via SNS, outbound via `send_text_message` |
| Outbound queue | Amazon SQS | Triggers customer/store outbound calls |
| Session storage | DynamoDB (24h TTL) | AgentSquad `DynamoDbChatStorage` |
| Auth cache | DynamoDB (24h TTL) | Tenant-aware compound key `{phone}:{to_phone}` (since v1.4.6) |
| Conversation log | DynamoDB (90-day TTL, GSI on user_id) | For PF compliance + audit |
| HTTP client | httpx.AsyncClient | All PF API calls |
| Retry | tenacity | `retry_bedrock`, `retry_secrets`, plus inline `_post_with_retry_on_5xx` for PF write endpoints |
| Linting | ruff | `uv run ruff check src/ tests/` |

### SSML and TTS do-nots

Following the v1.4.7 fix:

- **Never start a TTS payload with `<break>`** — leading SSML breaks cause ElevenLabs `eleven_turbo_v2_5` to leak phantom warm-up tokens (e.g. "Torii", "Japura") at stream start. Anchor with a real word first.
- Inter-word `<break time="Xs" />` is fine and used for natural pacing.
- Avoid more than 2-3 break tags per spoken sentence — ElevenLabs documents that excessive breaks cause artifacts.
- See `tests/unit/test_vapi.py::TestGreetingsDoNotStartWithSSMLBreak` for the regression guard.

---

## Quick start

```bash
# Install dependencies
uv sync

# Run dev server (uvicorn on :8000)
bash scripts/dev-server.sh

# Run all unit tests (~545 tests, ~17s)
uv run pytest tests/unit/ -v

# Run integration tests (requires AWS credentials)
AWS_PROFILE=pf-aws uv run pytest tests/integration/ -v -s

# Lint & format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

---

## Project structure

```
src/
├── main.py                        # FastAPI app entry point
├── config.py                      # Settings (pydantic-settings) + SecretsCache
├── auth/
│   ├── context.py                 # AuthContext (5 contextvars: token, client_id, customer_id, user_id, user_name, caller_type)
│   ├── phone_auth.py              # PF phone-call-login + DynamoDB cache (tenant-aware key)
│   └── office_hours.py            # Office-hours computation for greeting + transfer gating
├── channels/
│   ├── chat.py                    # POST /chat, /chat/stream (SSE)
│   ├── vapi.py                    # POST /vapi/webhook (assistant-request, tool-calls, status, end-of-call)
│   ├── vapi_llm.py                # POST /vapi/chat/completions (Custom LLM endpoint)
│   ├── vapi_config.py             # Vapi assistant ↔ phone mapping (DynamoDB + reverse lookup)
│   ├── sms.py                     # POST /sms/webhook (Pinpoint via SNS)
│   ├── outbound.py                # Outbound call API (status, trigger)
│   ├── outbound_consumer.py       # SQS consumer; relaxed required-field check for call_type
│   ├── outbound_store.py          # DynamoDB store for outbound call records
│   ├── outbound_vapi.py           # Vapi API client for outbound calls
│   ├── conversation_log.py        # Async conversation logging (DynamoDB)
│   ├── history.py                 # GET /conversations API endpoints
│   ├── admin.py                   # Admin endpoints (Vapi assistants, cache flush)
│   ├── formatters.py              # Channel-specific response formatting
│   └── schemas.py                 # Pydantic request/response models
├── orchestrator/
│   ├── __init__.py                # AgentSquad assembly
│   ├── response_utils.py          # extract_response_text()
│   ├── agents/                    # Agent definitions
│   │   ├── scheduling_agent.py    # 13 scheduling tools
│   │   ├── chitchat_agent.py
│   │   └── weather_agent.py
│   └── prompts/                   # System prompts per agent
│       └── scheduling_agent.py    # Anti-fabrication rules, no-projects + not-schedulable flows (v1.4.9)
├── tools/
│   ├── scheduling.py              # 13 async tool handlers + _post_with_retry_on_5xx helper (v1.4.9)
│   ├── weather.py                 # Open-Meteo weather API
│   ├── weather_aware.py           # Weather suitability analysis
│   ├── project_rules.py           # Project status business rules
│   ├── date_utils.py              # Natural-language date parsing (incl. weekday names since v1.4.9)
│   ├── pii_filter.py              # PII detection and filtering for store callers
│   └── api_client.py              # Shared PF API helpers
└── observability/
    ├── logging.py                 # Structured JSON logging + RequestContext
    ├── middleware.py              # Request logging middleware
    └── retry.py                   # Tenacity retry decorators (Bedrock, S3, Secrets)

tests/
├── conftest.py                    # Shared test fixtures
├── unit/                          # 545 unit tests across 25 test files
│   ├── test_vapi.py               # Vapi webhook, tool calls, blind transfer, greeting regression guards
│   ├── test_vapi_llm.py           # Custom LLM endpoint
│   ├── test_vapi_config.py
│   ├── test_chat.py               # Chat endpoints, SSE streaming
│   ├── test_scheduling_tools.py   # Scheduling tool handlers (incl. not_schedulable response)
│   ├── test_phone_auth.py         # Tenant-aware auth cache + caller_type propagation
│   ├── test_call_notes_retry.py   # _post_with_retry_on_5xx (v1.4.9)
│   ├── test_outbound.py           # Outbound call API
│   ├── test_outbound_consumer.py  # SQS consumer
│   ├── test_outbound_store.py     # Outbound DynamoDB store
│   ├── test_outbound_vapi.py      # Outbound Vapi client
│   ├── test_conversation_log.py
│   ├── test_history.py
│   ├── test_date_utils.py         # Date parsing (incl. weekday names)
│   ├── test_formatters.py
│   ├── test_office_hours.py
│   ├── test_weather_aware.py
│   ├── test_project_rules.py
│   ├── test_sms.py
│   ├── test_admin.py
│   ├── test_store_lookup.py
│   ├── test_welcome.py
│   ├── test_auth_context.py
│   └── test_response_utils.py
└── integration/                   # E2E tests against live APIs
    ├── scenarios.json
    ├── routing_scenarios.json
    ├── test_e2e_chat_api.py
    ├── test_scheduling_flows.py
    ├── test_structural.py
    ├── test_classifier_routing.py
    └── test_multi_turn.py

env_setup/                         # AWS infrastructure provisioning
├── env-config.sh                  # Environment config (VPC, subnets, naming)
├── 01-dynamodb.sh                 # DynamoDB tables (5 tables)
├── 02-iam-roles.sh                # IAM task + execution roles
├── 03-ecr.sh                      # ECR repository
├── 04-ecs-fargate.sh              # ECS cluster, task def, service, ALB
├── 05-secrets.sh                  # Secrets Manager entries
├── 06-sms.sh                      # SMS channel (AWS End User Messaging)
├── 07-deploy.sh                   # Build, push, deploy to dev/qa
├── 08-tag-resources.sh            # Tag all resources for cost allocation
└── prod/                          # Prod-specific provisioning (us-east-2)
    ├── env-config-prod.sh
    ├── 01-dynamodb.sh → 08-dns.sh

docs/
├── reqs/                          # Requirements + planning docs (gitignored)
│   ├── plans/                     # Time-stamped action plans (e.g. 2026-05-09-post-v1.4.8-call-analysis.md)
│   └── ...

scripts/
├── dev-server.sh                  # Run uvicorn locally
└── ...

test-client/                       # Browser-based test UI
```

---

## Scheduling tools

| Tool | Type | Description |
|------|------|-------------|
| `list_projects` | Read | List customer projects with optional category filter |
| `get_project_details` | Read | Detailed info for a specific project |
| `get_available_dates` | Read | Available scheduling dates (handles `already_scheduled` and `not_schedulable` PF responses) |
| `get_time_slots` | Read | Time slots for a specific date |
| `confirm_appointment` | Write | Confirm and schedule an appointment |
| `reschedule_appointment` | Write | Cancel existing and prepare for rescheduling |
| `cancel_appointment` | Write | Cancel an existing appointment (requires reason) |
| `add_note` | Write | Add a note to a project |
| `list_notes` | Read | List all notes for a project |
| `get_business_hours` | Read | Tenant office hours |
| `get_project_weather` | Read | Weather forecast + suitability analysis for project address |
| `get_installation_address` | Read | Installation address for a project |
| `update_installation_address` | Write | Update installation address (with confirmation) |

Plus internal end-of-call helpers (not exposed as tools):

- `post_call_summary_notes` — posts call summary + project notes to PF (with 5xx retry)
- `post_store_call_notes` — posts notes for store-caller flow

---

## Vapi phone channel

The phone channel uses Vapi's **server-URL mode** with a **Custom LLM endpoint**:

1. **Call starts** — Vapi sends `assistant-request` to `/vapi/webhook`.
2. **Phone auth** — `get_or_authenticate(from_phone, to_phone)` calls PF `phone-call-login`. Result is cached in DynamoDB with a tenant-aware compound key `{phone}:{to_phone}`.
3. **Caller-type routing** (since v1.4.7):
   - `caller_type=user` → personalised greeting with first name + custom-LLM scheduling assistant config
   - `caller_type=store` → store-caller assistant (PO/project-number lookup, status only, PII-scrubbed)
   - `caller_type=unknown` → lead-capture assistant (Phase 3A, gated on tenant flag) or fall-through to store
4. **Custom LLM** — Vapi routes all conversational reasoning to `POST /vapi/chat/completions` (our Bedrock-backed endpoint) — saves $0.02-0.20/min vs Vapi's built-in LLM.
5. **Tool calls** — Custom LLM calls `ask_scheduling_bot` (or `ask_store_bot` for store flow) → AgentSquad → tool handlers in `src/tools/scheduling.py`.
6. **Voice-optimised responses** — Markdown stripped, dates spelled out (e.g. "the twenty-ninth" not "29th"), no project IDs read aloud.
7. **Blind transfer** — Transfers to support use SIP REFER (blind-transfer mode) for reliability.
8. **Anti-fabrication guardrails** — Detects hallucinated `confirm` actions and fabricated time slots → forces actual API call or retry.
9. **End-of-call notes** — Call summary + project notes posted to PF (with 5xx retry since v1.4.9).

### Vapi assistant config (shared `_VOICE_CONFIG`)

| Setting | Value |
|---|---|
| `voice.provider` | `11labs` |
| `voice.model` | `eleven_turbo_v2_5` |
| `voice.voiceId` | `3liN8q8YoeB9Hk6AboKe` ("Lauren B.") |
| `voice.similarityBoost` | `0.75` |
| `voice.stability` | `0.5` |
| `transcriber.provider` | `deepgram` |
| `transcriber.model` | `nova-3` |
| `transcriber.endpointing` | `150` ms |
| `silenceTimeoutSeconds` | `30` (store: `60`) |
| `maxDurationSeconds` | `300` (5-min hard cap) |
| `startSpeakingPlan.waitSeconds` | `0.8` |
| `startSpeakingPlan.smartEndpointingEnabled` | `true` |
| `backgroundDenoisingEnabled` | `true` |

---

## Outbound calling

SQS-driven outbound call system for proactive customer and store scheduling.

1. **SQS message received** — Consumer reads from `pf-syn-schedulingagents-outbound-queue-{env}`.
2. **Call routing** — `call_type` field discriminates between scheduling, lead, and confirmation calls (Phase 3B/D-ready).
3. **Customer auth** — Existing customers use `phone-call-login`; new leads use a guest-auth path (Phase 3B).
4. **Multi-tenant caller ID** — Tenant phone is reverse-resolved to a Vapi UUID via `get_vapi_id_by_phone` (since v1.4.6) so calls show the correct caller ID.
5. **DynamoDB record** — Status tracked in `pf-syn-schedulingagents-outbound-calls-{env}` (30-day TTL).
6. **Vapi call** — `POST https://api.vapi.ai/call` with personalised assistant config.
7. **Status tracking** — Real-time status via `GET /outbound/{call_id}/status`.
8. **Voicemail + retry** — On no-answer, retry on alternate number. On voicemail, drop pre-recorded message.

---

## SMS channel

Inbound SMS handled by `POST /sms/webhook`, called by an SNS topic subscribed to AWS End User Messaging two-way replies.

> **Note (May 2026):** Only one production SMS number is registered in the AWS account: `+18786789053` (10DLC, owned by Window Treatments Unlimited tenant `19PF06WT`). It's configured for both inbound (via `pf-syn-sms-inbound-prod` SNS topic) and outbound. **No QA SMS infrastructure** exists for the scheduling bot yet — synthetic webhook simulation is required for QA SMS testing (see [Testing strategy](#testing-strategy)).

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a chat message, get bot response |
| `POST` | `/chat/stream` | SSE streaming chat response |
| `POST` | `/vapi/webhook` | Vapi phone channel webhook (assistant-request, tool-calls, status, end-of-call) |
| `POST` | `/vapi/chat/completions` | Custom LLM endpoint (Bedrock-backed, OpenAI-compatible) |
| `POST` | `/sms/webhook` | SMS inbound webhook (Pinpoint/SNS) |
| `GET` | `/outbound/{call_id}/status` | Get outbound call status |
| `GET` | `/outbound/calls` | List outbound calls |
| `POST` | `/outbound/trigger` | Manually trigger outbound call (dev/test) |
| `GET` | `/conversations` | List/search conversations (by user_id, date, channel) |
| `GET` | `/conversations/{session_id}` | Full conversation history for a session |
| `GET` | `/admin/vapi-assistants` | List registered Vapi assistants |
| `POST` | `/admin/vapi-assistants` | Register a Vapi assistant |
| `DELETE` | `/admin/vapi-assistants/{id}` | Remove a Vapi assistant config |
| `DELETE` | `/admin/phone-cache/{phone}` | Flush cached phone credentials |
| `POST` | `/auth/login` | Dev-only: PF login proxy |
| `GET` | `/health` | Health check |

---

## DynamoDB tables

| Table | Purpose | TTL | Key schema |
|-------|---------|-----|---|
| `pf-syn-schedulingagents-sessions-{env}` | AgentSquad conversation storage | 24h | `session_id` (S) |
| `pf-syn-schedulingagents-phone-creds-{env}` | Phone auth credential cache (tenant-aware key) + tenant config rows (`client:{client_id}`) | 24h (creds) / none (config) | `phone_number` (S) |
| `pf-syn-schedulingagents-conversations-{env}` | Conversation history/audit log | 90 days | `conversation_id` (S) + GSI on `user_id` |
| `pf-syn-schedulingagents-vapi-assistants-{env}` | Vapi assistant ↔ phone mapping (incl. reverse lookup) | None | `assistant_id` (S) |
| `pf-syn-schedulingagents-outbound-calls-{env}` | Outbound call records | 30 days | `call_id` (S) |

---

## Branch model

> **Default branch is `release`** (changed from `main` on 2026-05-09).

| Branch | Purpose | Push policy |
|---|---|---|
| `release` | Production-tracking. Whatever is here is what's running on prod (after deploy). | Fast-forward only. Tag every release. |
| `dev` | Integration branch. Features merge here first. | Fast-forward when possible. |
| `feature/*` | Per-feature branches. | Squash-merge into `dev`, then promote to `release` after validation. |
| `fix/*` | Hotfix branches off `release`. | Cherry-pick or fast-forward back to `release` + `dev`. |
| `phase3a/lead-capture-lite` | Phase 3A v0.1 work-in-progress. | Long-lived feature branch. |

There is **no** `main` branch — it was retired on 2026-05-09.

---

## Deployment

```bash
# Deploy to dev (us-east-1)
bash env_setup/07-deploy.sh dev

# Deploy to QA (us-east-1)
bash env_setup/07-deploy.sh qa

# Deploy to prod (us-east-2) — interactive yes/no confirmation
bash env_setup/prod/07-deploy.sh

# Tag resources for cost allocation
bash env_setup/08-tag-resources.sh qa
bash env_setup/08-tag-resources.sh prod

# Monitor a specific deployment
aws ecs describe-services --profile pf-aws --region us-east-1 \
  --cluster pf-syn-schedulingagents-cluster-qa \
  --services pf-syn-schedulingagents-bot-qa \
  --query 'services[0].deployments' --output table

# Tail logs
aws logs tail /ecs/pf-syn-schedulingagents-bot-prod \
  --profile pf-aws --region us-east-2 --follow
```

The deploy script:
1. Authenticates to ECR
2. Builds Docker image (`linux/amd64`)
3. Pushes to ECR with tag `release-{env}` (e.g. `release-prod`)
4. Triggers ECS rolling deployment via `update-service --force-new-deployment`

After deploy, **also tag the ECR image with the version number**:

```bash
NEW=$(AWS_PROFILE=pf-aws aws ecr batch-get-image --region us-east-2 \
  --repository-name pf-syn-schedulingagents-bot \
  --image-ids imageTag=release-prod --query 'images[0].imageManifest' --output text)
AWS_PROFILE=pf-aws aws ecr put-image --region us-east-2 \
  --repository-name pf-syn-schedulingagents-bot \
  --image-tag v1.4.9-prod --image-manifest "$NEW"
```

This preserves the previous image under its `vX.Y.Z-{env}` tag for rollback.

---

## Versioning + rollback

### Version scheme

`v{major}.{minor}.{patch}` git tags on the `release` branch tip after each prod deploy.

ECR images carry **two parallel tag families**:

| Tag family | Purpose | Example |
|---|---|---|
| `release-{env}` | Mutable pointer to the *currently-serving* image per environment | `release-prod`, `release-qa` |
| `v{X.Y.Z}-{env}` | Immutable per-version snapshot for rollback | `v1.4.9-prod`, `v1.4.8-prod` |

### Rollback procedure

To roll prod back to a previous version:

```bash
# 1. Re-point release-prod tag at the desired version's image
NEW=$(AWS_PROFILE=pf-aws aws ecr batch-get-image --region us-east-2 \
  --repository-name pf-syn-schedulingagents-bot \
  --image-ids imageTag=v1.4.8-prod \
  --query 'images[0].imageManifest' --output text)

AWS_PROFILE=pf-aws aws ecr put-image --region us-east-2 \
  --repository-name pf-syn-schedulingagents-bot \
  --image-tag release-prod --image-manifest "$NEW"

# 2. Trigger ECS to pull and roll
AWS_PROFILE=pf-aws aws ecs update-service --region us-east-2 \
  --cluster pf-syn-schedulingagents-cluster-prod \
  --service pf-syn-schedulingagents-svc-prod \
  --force-new-deployment

# 3. Wait for rollout (~3-5 min)
AWS_PROFILE=pf-aws aws ecs wait services-stable --region us-east-2 \
  --cluster pf-syn-schedulingagents-cluster-prod \
  --services pf-syn-schedulingagents-svc-prod
```

QA rollback is identical — substitute `qa` for `prod` and `us-east-1` for `us-east-2`.

---

## Testing strategy

Three modes, used in combination per fix:

### Mode 1 — Synthetic Vapi webhook (fastest)

Hand-craft Vapi-format JSON and POST it directly to the webhook. No phone needed. Best for server-side bug fixes.

```bash
curl -X POST https://schedulingagent.dev.projectsforce.com/vapi/webhook \
  -H "x-vapi-secret: $(aws secretsmanager get-secret-value --region us-east-1 \
    --secret-id vapi/api-key/dev --query SecretString --output text \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['vapi_api_key'])")" \
  -H "content-type: application/json" \
  -d '{"message":{"type":"end-of-call-report","endedReason":"customer-ended-call",...}}'
```

Used for: end-of-call note posting, status-update flows, tool-call routing logic.

### Mode 2 — Vapi outbound to a controlled number

Trigger a Vapi outbound call to a number you control (e.g., a Vonage line). Real audio + LLM in the loop. Costs ~$0.05-0.40 per call.

Used for: prompt iteration, LLM behaviour, anti-fabrication validation.

### Mode 3 — Inbound from any phone to QA

Dial the QA Vapi number from any phone (cell, Vonage softphone, etc.). Most realistic. Best as the final pre-prod gate.

QA Vapi numbers (verify via Vapi dashboard before testing):
- `+14588990940` (`SchedulingBot-QA`)
- `+17155004798` (`SchedulingBotQA-TV`)
- `+15705590511` (`SchedulingBotQA-TV`)

### Pre-existing test failures (known)

Two unit tests fail on every run; they are **pre-existing** (predate this hotfix work) and unrelated to current code:

- `tests/unit/test_scheduling_tools.py::TestGetTimeSlots::test_returns_slots`
- `tests/unit/test_vapi_llm.py::TestCustomLLMConfig::test_config_preserves_voice_and_transcriber`

Either retire or fix in a future PR.

---

## Environment

| Env | Region | URL | ECS cluster |
|---|---|---|---|
| **Dev** | us-east-1 | `https://schedulingagent.dev.projectsforce.com` | `pf-syn-schedulingagents-cluster-dev` |
| **QA** | us-east-1 | `https://schedulingagent.qa.projectsforce.com` | `pf-syn-schedulingagents-cluster-qa` |
| **Prod** | us-east-2 | `https://schedulingagent.apps.projectsforce.com` | `pf-syn-schedulingagents-cluster-prod` |

| | |
|---|---|
| **AWS profile** | `pf-aws` |
| **AWS account** | `772634497954` |
| **Resource naming** | `pf-syn-schedulingagents-{resource}-{env}` |

### AWS Resource Tagging

All resources tagged for cost allocation via `env_setup/08-tag-resources.sh`:

| Tag | Value | Purpose |
|-----|-------|---------|
| `Project` | `pf-syn` | Cost allocation across all SchedulingAIBot resources |
| `Application` | `schedulingagents` | App-level drill-down |
| `Environment` | `qa` / `prod` | Per-environment cost split |
| `ManagedBy` | `scripts` | Distinguish from console-created resources |

---

## Version history

| Tag | Date | Highlights |
|---|---|---|
| **`v1.4.9`** | 2026-05-09 | **Six fixes from prod call analysis:** PF 5xx retry on call notes; weekday name parsing ("this Thursday"); `not_schedulable` 400 handling; prompt anti-fabrication strengthened; no-projects UX; reschedule recovery attaches fresh dates; warning-vs-info logging cleanup. |
| **`v1.4.8`** | 2026-05-09 | Tenant-aware `get_cached_auth` lookup — fixes silent end-of-call note posting failures introduced by v1.4.6 multi-tenant cache isolation. |
| **`v1.4.7`** | 2026-05-08 | Removes leading SSML `<break>` from greetings — eliminates ElevenLabs phantom-word artifacts ("Torii", "Japura") at greeting start. |
| **`v1.4.6`** | 2026-05-05 | Multi-tenant outbound caller ID via DynamoDB reverse lookup; relaxed status gate; `dynamodb:Scan` IAM permission. |
| `v1.4.5` | 2026-04-26 | Production infrastructure scripts and DNS setup (us-east-2). |
| `v1.4.4` | (earlier) | (see git log) |

Full history: `git log --oneline` on the `release` branch.

---

## Operations & troubleshooting

### Recent action plans (gitignored, on disk only)

- `docs/reqs/plans/2026-05-09-post-v1.4.8-call-analysis.md` — 24-call prod analysis + Wave 1/2 hotfix plan
- `docs/reqs/plans/2026-05-09-pf-ticket-store-login-5xx.md` — PF API instability ticket draft
- `docs/reqs/plans/phase3-api-findings.md` — Phase 3 lead-capture API findings + sprint plan

### Quick commands

```bash
# Inspect a prod call by Vapi call ID
AWS_PROFILE=pf-aws aws logs tail /ecs/pf-syn-schedulingagents-bot-prod \
  --region us-east-2 --since 1h --filter-pattern "<call-id-prefix>"

# Pull a Vapi call object (transcript, cost, recording)
curl -s "https://api.vapi.ai/call/<call-id>" \
  -H "Authorization: Bearer $(aws secretsmanager get-secret-value \
    --region us-east-2 --secret-id vapi/api-key/prod \
    --query SecretString --output text \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['vapi_private_key'])")"

# Flush cached phone credentials (force re-auth on next call)
curl -X DELETE https://schedulingagent.apps.projectsforce.com/admin/phone-cache/+15551234567

# Check ECS service health
AWS_PROFILE=pf-aws aws ecs describe-services --region us-east-2 \
  --cluster pf-syn-schedulingagents-cluster-prod \
  --services pf-syn-schedulingagents-svc-prod \
  --query 'services[0].[serviceName,desiredCount,runningCount,deployments[?status==`PRIMARY`].rolloutState[]]' \
  --output text
```

### Known external dependencies

- **PF CX Portal API** (`api-cx-portal.{env}.projectsforce.com`) — All scheduling, project, customer, store-login operations. Occasional 5xx (especially on `/store-login` per the May 8 incident); retry-on-5xx is added on the call-notes endpoint as of v1.4.9.
- **ElevenLabs** (via Vapi) — TTS. Subject to documented behaviours around SSML breaks and trained-voice mannerisms.
- **OpenAI GPT-5.2** (via Vapi) — Vapi-side reasoning. Occasional fabrication of time slots and confirm actions; guardrails catch and retry.
- **Amazon Bedrock (Anthropic Sonnet 4)** — Backend reasoning via AgentSquad.
- **Open-Meteo** — Weather forecasts. No auth, free tier.

---

## License & ownership

Internal — ProjectsForce. Code owners: Jay Jayakeerthy (`jjayaraj@gmail.com`).
