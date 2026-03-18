#!/bin/bash
# Create DynamoDB tables for the PF Scheduling Bot.
#
# Tables:
#   pf-syn-schedulingagents-sessions-{env}       — AgentSquad conversation storage
#   pf-syn-schedulingagents-phone-creds-{env}    — Phone auth credential cache
#
# Usage:
#   bash env_setup/01-dynamodb.sh          # default: dev
#   ENVIRONMENT=prod bash env_setup/01-dynamodb.sh

set -euo pipefail

PROFILE="${AWS_PROFILE:-pf-aws}"
REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"

echo "Creating DynamoDB tables (env=$ENV, region=$REGION, profile=$PROFILE)"
echo ""

# ── 1. Session storage table ──────────────────────────────────────────
SESSION_TABLE="pf-syn-schedulingagents-sessions-${ENV}"

echo "▶ Creating $SESSION_TABLE..."
aws dynamodb create-table \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$SESSION_TABLE" \
  --attribute-definitions \
    AttributeName=PK,AttributeType=S \
    AttributeName=SK,AttributeType=S \
  --key-schema \
    AttributeName=PK,KeyType=HASH \
    AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
  2>/dev/null && echo "  ✓ Created" || echo "  → Already exists"

echo "  Enabling TTL on attribute 'ttl'..."
aws dynamodb update-time-to-live \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$SESSION_TABLE" \
  --time-to-live-specification Enabled=true,AttributeName=ttl \
  2>/dev/null && echo "  ✓ TTL enabled" || echo "  → TTL already enabled"

echo ""

# ── 2. Phone credentials cache table ──────────────────────────────────
PHONE_TABLE="pf-syn-schedulingagents-phone-creds-${ENV}"

echo "▶ Creating $PHONE_TABLE..."
aws dynamodb create-table \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$PHONE_TABLE" \
  --attribute-definitions \
    AttributeName=phone_number,AttributeType=S \
  --key-schema \
    AttributeName=phone_number,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
  2>/dev/null && echo "  ✓ Created" || echo "  → Already exists"

echo "  Enabling TTL on attribute 'ttl'..."
aws dynamodb update-time-to-live \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$PHONE_TABLE" \
  --time-to-live-specification Enabled=true,AttributeName=ttl \
  2>/dev/null && echo "  ✓ TTL enabled" || echo "  → TTL already enabled"

# ── 3. Conversations log table ────────────────────────────────────────
CONVERSATIONS_TABLE="pf-syn-schedulingagents-conversations-${ENV}"

echo "▶ Creating $CONVERSATIONS_TABLE..."
aws dynamodb create-table \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$CONVERSATIONS_TABLE" \
  --attribute-definitions \
    AttributeName=session_id,AttributeType=S \
    AttributeName=SK,AttributeType=S \
    AttributeName=user_id,AttributeType=S \
    AttributeName=created_at,AttributeType=S \
  --key-schema \
    AttributeName=session_id,KeyType=HASH \
    AttributeName=SK,KeyType=RANGE \
  --global-secondary-indexes \
    '[{
      "IndexName": "user-conversations-index",
      "KeySchema": [
        {"AttributeName": "user_id", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }]' \
  --billing-mode PAY_PER_REQUEST \
  --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
  2>/dev/null && echo "  ✓ Created" || echo "  → Already exists"

echo "  Enabling TTL on attribute 'ttl'..."
aws dynamodb update-time-to-live \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$CONVERSATIONS_TABLE" \
  --time-to-live-specification Enabled=true,AttributeName=ttl \
  2>/dev/null && echo "  ✓ TTL enabled" || echo "  → TTL already enabled"

# ── 4. Vapi assistant config table ───────────────────────────────────
VAPI_TABLE="pf-syn-schedulingagents-vapi-assistants-${ENV}"

echo "▶ Creating $VAPI_TABLE..."
aws dynamodb create-table \
  --profile "$PROFILE" \
  --region "$REGION" \
  --table-name "$VAPI_TABLE" \
  --attribute-definitions \
    AttributeName=assistant_id,AttributeType=S \
  --key-schema \
    AttributeName=assistant_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
  2>/dev/null && echo "  ✓ Created" || echo "  → Already exists"

echo ""
echo "Done. Tables:"
echo "  Sessions:       $SESSION_TABLE"
echo "  Phone creds:    $PHONE_TABLE"
echo "  Conversations:  $CONVERSATIONS_TABLE"
echo "  Vapi config:    $VAPI_TABLE"
