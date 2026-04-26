#!/bin/bash
# Configure SQS queues and SMS (AWS End User Messaging) for PRODUCTION (us-east-2).
# Isolated from dev/qa — sources env-config-prod.sh only.
#
# Creates:
#   - SQS dead-letter queue (DLQ)
#   - SQS outbound queue (with DLQ redrive policy)
#   - SNS topic for inbound SMS
#   - SMS configuration set
#
# Usage:
#   bash env_setup/prod/06-sms.sh
#   WEBHOOK_URL=https://schedulingagent.apps.projectsforce.com/sms/webhook bash env_setup/prod/06-sms.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/env-config-prod.sh"

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

DLQ_NAME="${PROJECT_PREFIX}-outbound-dlq-${ENVIRONMENT}"
QUEUE_NAME="${PROJECT_PREFIX}-outbound-queue-${ENVIRONMENT}"
SNS_TOPIC_NAME="${PROJECT_PREFIX}-sms-inbound-${ENVIRONMENT}"
SMS_CONFIG_SET="scheduling-agent-sms-config-${ENVIRONMENT}"

echo "SQS + SMS Setup (PRODUCTION, region=$AWS_REGION)"
echo ""

# ── 1. SQS Dead-Letter Queue ──────────────────────────────────────────
echo "▶ Checking DLQ: $DLQ_NAME"
DLQ_URL=$(aws sqs get-queue-url \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --queue-name "$DLQ_NAME" \
  --query 'QueueUrl' --output text 2>/dev/null || echo "")

if [ -z "$DLQ_URL" ] || [ "$DLQ_URL" = "None" ]; then
  DLQ_URL=$(aws sqs create-queue \
    --profile "$PROFILE" \
    --region "$AWS_REGION" \
    --queue-name "$DLQ_NAME" \
    --attributes '{"MessageRetentionPeriod":"1209600"}' \
    --tags Project="${PROJECT_PREFIX}-bot",Environment="$ENVIRONMENT" \
    --query 'QueueUrl' --output text)
  echo "  ✓ DLQ created: $DLQ_URL"
else
  echo "  → DLQ exists: $DLQ_URL"
fi

DLQ_ARN=$(aws sqs get-queue-attributes \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --queue-url "$DLQ_URL" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)
echo ""

# ── 2. SQS Outbound Queue (with DLQ redrive) ──────────────────────────
echo "▶ Checking outbound queue: $QUEUE_NAME"
QUEUE_URL=$(aws sqs get-queue-url \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --queue-name "$QUEUE_NAME" \
  --query 'QueueUrl' --output text 2>/dev/null || echo "")

if [ -z "$QUEUE_URL" ] || [ "$QUEUE_URL" = "None" ]; then
  QUEUE_URL=$(aws sqs create-queue \
    --profile "$PROFILE" \
    --region "$AWS_REGION" \
    --queue-name "$QUEUE_NAME" \
    --attributes "{
      \"MessageRetentionPeriod\": \"345600\",
      \"VisibilityTimeout\": \"300\",
      \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"
    }" \
    --tags Project="${PROJECT_PREFIX}-bot",Environment="$ENVIRONMENT" \
    --query 'QueueUrl' --output text)
  echo "  ✓ Queue created: $QUEUE_URL"
else
  echo "  → Queue exists: $QUEUE_URL"
fi
echo ""

# ── 3. SNS Topic for inbound SMS ──────────────────────────────────────
echo "▶ Creating SNS topic: $SNS_TOPIC_NAME"
TOPIC_ARN=$(aws sns create-topic \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --name "$SNS_TOPIC_NAME" \
  --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
  --query 'TopicArn' --output text)
echo "  ✓ Topic ARN: $TOPIC_ARN"
echo ""

# ── 4. Subscribe webhook to SNS topic ─────────────────────────────────
if [ -n "${WEBHOOK_URL:-}" ]; then
  echo "▶ Subscribing webhook: $WEBHOOK_URL"
  SUB_ARN=$(aws sns subscribe \
    --profile "$PROFILE" \
    --region "$AWS_REGION" \
    --topic-arn "$TOPIC_ARN" \
    --protocol https \
    --notification-endpoint "$WEBHOOK_URL" \
    --query 'SubscriptionArn' --output text)
  echo "  ✓ Subscription: $SUB_ARN"
else
  echo "⚠ WEBHOOK_URL not set. Subscribe manually:"
  echo ""
  echo "  aws sns subscribe \\"
  echo "    --profile $PROFILE --region $AWS_REGION \\"
  echo "    --topic-arn $TOPIC_ARN \\"
  echo "    --protocol https \\"
  echo "    --notification-endpoint https://schedulingagent.apps.projectsforce.com/sms/webhook"
fi
echo ""

# ── 5. SMS configuration set ──────────────────────────────────────────
echo "▶ Checking SMS configuration set: $SMS_CONFIG_SET"
CONFIG_EXISTS=$(aws pinpoint-sms-voice-v2 describe-configuration-sets \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --configuration-set-names "$SMS_CONFIG_SET" \
  --query 'ConfigurationSets[0].ConfigurationSetName' --output text 2>/dev/null || echo "")

if [ -n "$CONFIG_EXISTS" ] && [ "$CONFIG_EXISTS" != "None" ]; then
  echo "  ✓ Configuration set exists"
else
  echo "  Creating configuration set..."
  aws pinpoint-sms-voice-v2 create-configuration-set \
    --profile "$PROFILE" \
    --region "$AWS_REGION" \
    --configuration-set-name "$SMS_CONFIG_SET" \
    --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
    > /dev/null 2>&1 && echo "  ✓ Created" || echo "  → Check if it already exists under another name"
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  SQS + SMS Setup Summary (PRODUCTION)"
echo ""
echo "  DLQ:                $DLQ_URL"
echo "  Outbound Queue:     $QUEUE_URL"
echo "  SNS Topic:          $TOPIC_ARN"
echo "  Config Set:         $SMS_CONFIG_SET"
echo ""
echo "  ECS task env vars:"
echo "    OUTBOUND_QUEUE_URL=$QUEUE_URL"
echo "    SMS_ORIGINATION_NUMBER=<prod-number>"
echo "    SMS_CONFIGURATION_SET=$SMS_CONFIG_SET"
echo "═══════════════════════════════════════════════════════════"
