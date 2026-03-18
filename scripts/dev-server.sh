#!/bin/bash
# Start the dev server locally with AWS dev environment credentials.
#
# Usage:
#   bash scripts/dev-server.sh              # default port 8000
#   bash scripts/dev-server.sh 8001         # custom port

set -euo pipefail

PROFILE="pf-aws"
REGION="us-east-1"
PORT="${1:-8000}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║         PF Scheduling Bot — Dev Server                  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Verify AWS credentials ────────────────────────────────────
echo "▶ Checking AWS credentials (profile: $PROFILE)..."

IDENTITY=$(aws sts get-caller-identity --profile "$PROFILE" --output json 2>&1)
if [ $? -ne 0 ]; then
  echo ""
  echo "ERROR: AWS credentials not configured or expired."
  echo "  Run: aws sso login --profile $PROFILE"
  echo "  Or:  aws configure --profile $PROFILE"
  exit 1
fi

ACCOUNT=$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
USER_ARN=$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
echo "  Account: $ACCOUNT"
echo "  User:    $USER_ARN"
echo ""

# ── 2. Set environment ───────────────────────────────────────────
# AWS_PROFILE is sufficient — boto3 picks up SSO sessions, static creds,
# and assumed-role credentials automatically.  No need to extract keys.
export AWS_PROFILE="$PROFILE"
export AWS_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"
export ENVIRONMENT="${ENVIRONMENT:-dev}"
export DEV_SERVER=true
export USE_DYNAMODB_STORAGE=false
# PF_API_BASE_URL auto-derived from ENVIRONMENT (dev/staging/prod)
# Override: export PF_API_BASE_URL="https://api-cx-portal.apps.projectsforce.com"
export VAPI_SECRET_ARN=""
export SMS_ORIGINATION_NUMBER=""
# SMS_CONFIGURATION_SET auto-derived from ENVIRONMENT (scheduling-agent-sms-config-{env})

echo "▶ Starting uvicorn on http://127.0.0.1:$PORT"
echo ""
echo "  Endpoints:"
echo "    Health:      http://127.0.0.1:$PORT/health"
echo "    Chat API:    http://127.0.0.1:$PORT/chat"
echo "    Chat SSE:    http://127.0.0.1:$PORT/chat/stream"
echo "    Vapi:        http://127.0.0.1:$PORT/vapi/webhook"
echo "    SMS:         http://127.0.0.1:$PORT/sms/webhook"
echo "    Chat test:   http://127.0.0.1:$PORT/test/chat-test.html"
echo "    API Docs:    http://127.0.0.1:$PORT/docs"
echo ""
echo "  DynamoDB storage: OFF (in-memory — set USE_DYNAMODB_STORAGE=true after"
echo "    creating pf-syn-schedulingagents-sessions table)"
echo ""
echo "  Press Ctrl+C to stop"
echo "─────────────────────────────────────────────────────────────"
echo ""

uv run python -m uvicorn main:app \
  --app-dir "$PROJECT_DIR/src" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --reload \
  --reload-dir "$PROJECT_DIR/src"
