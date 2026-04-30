# ProjectsForce Scheduling AI Bot

AI-powered scheduling assistant for ProjectsForce 360 field service management. Handles appointment scheduling, rescheduling, cancellation, weather-aware recommendations, and project inquiries across web chat, phone (Vapi), SMS, and outbound calling channels.

## Architecture

Four channels, one AgentSquad orchestrator with three agents:

```
  WEB CHAT             PHONE (Vapi)           SMS (Pinpoint)       OUTBOUND (SQS)
  POST /chat           POST /vapi/webhook     POST /sms/webhook    SQS Consumer
       |                    |                       |                    |
       |                    | assistant-request      |                    |
       |                    | (dynamic greeting,     |                    |
       |                    |  phone auth, office    |                    |
       |                    |  hours, blind transfer)|                    |
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
        (12 tools)        (no tools)        (1 tool)
              |                                 |
              v                                 v
        PF CX Portal                      Open-Meteo
           API                               API
```

| Agent | Purpose | Tools |
|-------|---------|-------|
| **Scheduling Agent** (default) | Project listing, scheduling, rescheduling, cancellation, notes, address, weather | 12 tools via PF CX Portal API + Open-Meteo |
| **Chitchat Agent** | Greetings, help, small talk | None |
| **Weather Agent** | Weather forecasts for project locations | Open-Meteo API |

## Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI on ECS Fargate (uvicorn)
- **Package Manager:** UV
- **Orchestration:** [AgentSquad](https://github.com/awslabs/agent-squad) (`agent-squad[aws]`)
- **LLM:** Claude Sonnet 4 on Amazon Bedrock
- **Voice:** ElevenLabs `eleven_turbo_v2_5` via Vapi (STT: Deepgram Nova-3)
- **Session Storage:** DynamoDB (24h TTL)
- **Conversation Log:** DynamoDB (90-day TTL, GSI on user_id)
- **Phone:** Vapi.ai (telephony, IVR, SIP, dynamic assistant config, Custom LLM endpoint, blind transfer)
- **SMS:** Amazon Pinpoint (inbound via SNS, outbound via send_messages)
- **Outbound Calls:** SQS queue + Vapi API (customer and store scheduling calls)
- **HTTP Client:** httpx.AsyncClient
- **Retry:** tenacity
- **Linting:** ruff

## Quick Start

```bash
# Install dependencies
uv sync

# Run dev server
bash scripts/dev-server.sh

# Run all unit tests (~500 tests)
uv run pytest tests/unit/ -v

# Run integration tests (requires AWS credentials)
AWS_PROFILE=pf-aws uv run pytest tests/integration/ -v -s

# Lint & format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## Project Structure

```
src/
├── main.py                        # FastAPI app entry point
├── config.py                      # Settings (pydantic-settings)
├── auth/
│   ├── context.py                 # AuthContext (contextvars)
│   └── phone_auth.py              # Phone-based auth (PF API + DynamoDB cache)
├── channels/
│   ├── chat.py                    # POST /chat, /chat/stream (SSE)
│   ├── vapi.py                    # POST /vapi/webhook (phone + assistant-request)
│   ├── vapi_llm.py                # POST /vapi/chat/completions (Custom LLM endpoint)
│   ├── vapi_config.py             # Vapi assistant-to-phone mapping (DynamoDB)
│   ├── sms.py                     # POST /sms/webhook (SMS)
│   ├── outbound.py                # Outbound call API (status, trigger)
│   ├── outbound_consumer.py       # SQS consumer for outbound calls
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
│   │   ├── scheduling_agent.py    # 12 scheduling tools
│   │   ├── chitchat_agent.py      # Casual conversation
│   │   └── weather_agent.py       # Weather forecasts
│   └── prompts/                   # System prompts per agent
├── tools/
│   ├── scheduling.py              # 12 async tool handlers
│   ├── weather.py                 # Open-Meteo weather API (current + target date)
│   ├── weather_aware.py           # Weather suitability analysis for scheduling
│   ├── project_rules.py           # Project status business rules
│   ├── date_utils.py              # Natural language date parsing
│   ├── pii_filter.py              # PII detection and filtering
│   └── api_client.py              # Shared PF API helpers
└── observability/
    ├── logging.py                 # Structured JSON logging
    ├── middleware.py               # Request logging middleware
    └── retry.py                   # Tenacity retry decorators

tests/
├── conftest.py                    # Shared test fixtures
├── unit/                          # ~500 unit tests across 27 test files
│   ├── test_vapi.py               # Vapi webhook, tool calls, blind transfer, guardrails
│   ├── test_vapi_llm.py           # Custom LLM endpoint, filler, transfer detection
│   ├── test_vapi_config.py        # Vapi assistant config
│   ├── test_chat.py               # Chat endpoints, SSE streaming
│   ├── test_scheduling_tools.py   # Scheduling tool handlers
│   ├── test_phone_auth.py         # Phone auth + credential caching
│   ├── test_outbound.py           # Outbound call API
│   ├── test_outbound_consumer.py  # SQS consumer
│   ├── test_outbound_store.py     # Outbound DynamoDB store
│   ├── test_outbound_vapi.py      # Outbound Vapi client
│   ├── test_conversation_log.py   # Conversation logging
│   ├── test_history.py            # Conversation history endpoints
│   ├── test_date_utils.py         # Date parsing
│   ├── test_formatters.py         # Response formatting
│   ├── test_office_hours.py       # Business hours logic
│   ├── test_weather_aware.py      # Weather suitability
│   ├── test_project_rules.py      # Project status rules
│   ├── test_sms.py                # SMS webhook
│   ├── test_admin.py              # Admin endpoints
│   ├── test_store_lookup.py       # Store lookup
│   ├── test_welcome.py            # Welcome/greeting handler
│   ├── test_auth_context.py       # AuthContext get/set/clear
│   └── test_response_utils.py     # Response text extraction
└── integration/                   # E2E tests against live APIs
    ├── scenarios.json             # Parametrized test scenarios
    ├── routing_scenarios.json     # Classifier routing test data
    ├── test_e2e_chat_api.py       # Full /chat API E2E tests
    ├── test_scheduling_flows.py   # Direct tool call tests
    ├── test_structural.py         # Structural scheduling flow tests
    ├── test_classifier_routing.py # Agent routing accuracy tests
    └── test_multi_turn.py         # Multi-turn conversation tests

env_setup/                         # AWS infrastructure provisioning
├── env-config.sh                  # Environment config (VPC, subnets, naming)
├── env-config-prod.sh             # Prod-specific config (us-east-2)
├── 01-dynamodb.sh                 # DynamoDB tables (5 tables)
├── 02-iam-roles.sh                # IAM task + execution roles
├── 03-ecr.sh                      # ECR repository
├── 04-ecs-fargate.sh              # ECS cluster, task def, service, ALB
├── 05-secrets.sh                  # Secrets Manager entries
├── 06-sms.sh                      # SMS channel (AWS End User Messaging)
├── 07-deploy.sh                   # Build, push, deploy to ECS
├── 08-tag-resources.sh            # Tag all resources for cost allocation
└── prod/                          # Prod-specific provisioning (us-east-2)
    ├── 01-dynamodb.sh → 08-dns.sh # Mirror scripts for prod region

test-client/                       # Browser-based test UI
```

## Scheduling Tools

| Tool | Type | Description |
|------|------|-------------|
| `list_projects` | Read | List customer projects with optional filters |
| `get_project_details` | Read | Get detailed info for a specific project |
| `get_available_dates` | Read | Get available scheduling dates |
| `get_time_slots` | Read | Get time slots for a specific date |
| `confirm_appointment` | Write | Confirm and schedule an appointment |
| `reschedule_appointment` | Write | Cancel existing and prepare for rescheduling |
| `cancel_appointment` | Write | Cancel an existing appointment |
| `add_note` | Write | Add a note to a project |
| `list_notes` | Read | List all notes for a project |
| `get_business_hours` | Read | Get business hours for the service provider |
| `get_project_weather` | Read | Get weather forecast + suitability analysis for project address |
| `get_installation_address` | Read | Get installation address for a project |

## Vapi Phone Channel

The phone channel uses Vapi's **server-URL mode** with a **Custom LLM endpoint**:

1. **Call starts** — Vapi sends `assistant-request` to our webhook
2. **Phone auth** — We authenticate the caller by phone number via PF API (`get_or_authenticate`)
3. **Personalized greeting** — Full assistant config with caller's first name, office hours awareness, per-tenant support number
4. **Custom LLM** — Vapi routes all reasoning to `POST /vapi/chat/completions` (our Bedrock Claude endpoint), replacing Vapi's built-in LLM — saves $0.02-0.20/min in LLM surcharges
5. **Tool calls** — Custom LLM calls `ask_scheduling_bot` for every user request, routed through AgentSquad
6. **Voice-optimized responses** — Markdown stripped, concise conversational output
7. **Blind transfer** — Transfers to support use SIP REFER (blind-transfer mode) for reliability
8. **Booking confirmation guardrail** — Detects hallucinated confirmations and forces actual `confirm_appointment` API call
9. **End-of-call notes** — Call summary and conversation log posted to PF API automatically

Key settings: ElevenLabs `eleven_turbo_v2_5` voice, Deepgram Nova-3 transcriber (150ms endpointing), filler: "One moment please." (on tool calls only), 30s silence timeout, 600s max duration, 8 end-call phrases.

## Outbound Calling

SQS-driven outbound call system for proactive customer and store scheduling:

1. **SQS message received** — Consumer picks up call request from outbound queue
2. **Customer auth** — Authenticates customer via phone-call-login API
3. **DynamoDB record** — Creates outbound call record with status tracking (30-day TTL)
4. **Vapi call** — Initiates call via Vapi API with personalized assistant config
5. **Status tracking** — Real-time status via API (`/outbound/{call_id}/status`)

Two call types: **customer outbound** (scheduling reminders) and **store outbound** (store-initiated scheduling).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a chat message, get bot response |
| `POST` | `/chat/stream` | SSE streaming chat response |
| `POST` | `/vapi/webhook` | Vapi phone channel webhook (assistant-request + tool calls) |
| `POST` | `/vapi/chat/completions` | Custom LLM endpoint (Bedrock Claude, OpenAI-compatible) |
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

## DynamoDB Tables

| Table | Purpose | TTL |
|-------|---------|-----|
| `pf-syn-schedulingagents-sessions-{env}` | AgentSquad conversation storage | 24h |
| `pf-syn-schedulingagents-phone-creds-{env}` | Phone auth credential cache | 24h |
| `pf-syn-schedulingagents-conversations-{env}` | Conversation history/audit log (+ GSI) | 90 days |
| `pf-syn-schedulingagents-vapi-assistants-{env}` | Vapi assistant-to-phone mapping | None |
| `pf-syn-schedulingagents-outbound-calls-{env}` | Outbound call records | 30 days |

## AWS Resource Tagging

All resources are tagged for cost allocation using `env_setup/08-tag-resources.sh`:

| Tag | Value | Purpose |
|-----|-------|---------|
| `Project` | `pf-syn` | Cost allocation across all SchedulingAIBot resources |
| `Application` | `schedulingagents` | App-level drill-down |
| `Environment` | `qa` / `prod` | Per-environment cost split |
| `ManagedBy` | `scripts` | Distinguish from console-created resources |

## Deployment

```bash
# Deploy to dev ECS (builds Docker, pushes to ECR, triggers rolling deploy)
bash env_setup/07-deploy.sh dev

# Deploy to QA
bash env_setup/07-deploy.sh qa

# Deploy to prod (us-east-2)
bash env_setup/prod/07-deploy.sh

# Tag resources for cost allocation
bash env_setup/08-tag-resources.sh qa
bash env_setup/08-tag-resources.sh prod

# Monitor deployment
aws ecs describe-services --profile pf-aws --region us-east-1 \
  --cluster pf-syn-schedulingagents-cluster-dev \
  --services pf-syn-schedulingagents-bot-dev \
  --query 'services[0].deployments' --output table

# Tail logs
aws logs tail /ecs/pf-syn-schedulingagents-bot-dev \
  --profile pf-aws --region us-east-1 --follow
```

## Environment

- **Dev:** `https://schedulingagent.dev.projectsforce.com`
- **QA:** `https://schedulingagent.qa.projectsforce.com`
- **Prod:** `https://schedulingagent.apps.projectsforce.com`
- **AWS Profile:** `pf-aws`
- **Region:** us-east-1 (dev/QA), us-east-2 (prod)
- **Resource Naming:** `pf-syn-schedulingagents-{resource}-{env}`
