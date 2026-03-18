# ProjectsForce Scheduling AI Bot

AI-powered scheduling assistant for ProjectsForce 360 field service management. Handles appointment scheduling, rescheduling, cancellation, and project inquiries across web chat, phone (Vapi), and SMS channels.

## Architecture

Three channels, one AgentSquad orchestrator with three agents:

```
  WEB CHAT             PHONE (Vapi)           SMS (Pinpoint)
  POST /chat           POST /vapi/webhook     POST /sms/webhook
       │                    │                       │
       └────────────────────┼───────────────────────┘
                            ▼
                    ┌───────────────┐
                    │  AgentSquad   │
                    │  Orchestrator │
                    └───────┬───────┘
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        Scheduling     Chitchat      Weather
         Agent          Agent         Agent
        (10 tools)    (no tools)    (1 tool)
              │                       │
              ▼                       ▼
        PF CX Portal            Open-Meteo
           API                     API
```

| Agent | Purpose | Tools |
|-------|---------|-------|
| **Scheduling Agent** (default) | Project listing, scheduling, rescheduling, cancellation, notes | 10 tools via PF CX Portal API |
| **Chitchat Agent** | Greetings, help, small talk | None |
| **Weather Agent** | Weather forecasts for project locations | Open-Meteo API |

## Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI on ECS Fargate (uvicorn)
- **Package Manager:** UV
- **Orchestration:** [AgentSquad](https://github.com/awslabs/agent-squad) (`agent-squad[aws]`)
- **LLM:** Claude Sonnet 4 on Amazon Bedrock
- **Session Storage:** DynamoDB (24h TTL)
- **Phone:** Vapi.ai (telephony, IVR, SIP)
- **SMS:** Amazon Pinpoint (inbound via SNS, outbound via send_messages)
- **HTTP Client:** httpx.AsyncClient
- **Retry:** tenacity
- **Linting:** ruff

## Quick Start

```bash
# Install dependencies
uv sync

# Run dev server
bash scripts/dev-server.sh

# Run all tests
uv run pytest tests/ -v

# Run unit tests only
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
│   ├── vapi.py                    # POST /vapi/webhook (phone)
│   ├── sms.py                     # POST /sms/webhook (SMS)
│   ├── formatters.py              # Channel-specific response formatting
│   └── schemas.py                 # Pydantic request/response models
├── orchestrator/
│   ├── __init__.py                # AgentSquad assembly
│   ├── response_utils.py          # extract_response_text()
│   ├── agents/                    # Agent definitions
│   │   ├── scheduling_agent.py    # 10 scheduling tools
│   │   ├── chitchat_agent.py      # Casual conversation
│   │   └── weather_agent.py       # Weather forecasts
│   └── prompts/                   # System prompts per agent
├── tools/
│   ├── scheduling.py              # 10 async tool handlers
│   ├── weather.py                 # Open-Meteo weather API
│   ├── project_rules.py           # Project status business rules
│   ├── date_utils.py              # Natural language date parsing
│   └── api_client.py              # Shared PF API helpers
└── observability/
    ├── logging.py                 # Structured JSON logging
    ├── middleware.py               # Request logging middleware
    └── retry.py                   # Tenacity retry decorators

tests/
├── conftest.py                    # Shared test fixtures
├── unit/                          # 268 unit tests
└── integration/                   # E2E tests against live APIs
    ├── scenarios.json             # Parametrized test scenarios
    ├── test_e2e_chat_api.py       # Full /chat API E2E tests
    ├── test_scheduling_flows.py   # Direct tool call tests
    ├── test_structural.py         # Structural scheduling flow tests
    ├── test_classifier_routing.py # Agent routing accuracy tests
    └── test_multi_turn.py         # Multi-turn conversation tests

env_setup/                         # AWS infrastructure provisioning
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
| `get_project_weather` | Read | Get weather forecast for project address |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a chat message, get bot response |
| `POST` | `/chat/stream` | SSE streaming chat response |
| `POST` | `/vapi/webhook` | Vapi phone channel webhook |
| `POST` | `/sms/webhook` | SMS inbound webhook (Pinpoint/SNS) |
| `GET` | `/health` | Health check |
| `GET` | `/test/chat-test.html` | Browser test UI |

## Deployment

```bash
# Deploy to ECS (builds Docker, pushes to ECR, triggers rolling deploy)
bash env_setup/07-deploy.sh

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
- **AWS Profile:** `pf-aws`
- **Region:** us-east-1 (dev), us-east-2 (prod)
- **Resource Naming:** `pf-syn-schedulingagents-{resource}-{env}`
