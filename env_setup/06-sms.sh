#!/bin/bash
# Configure SMS (AWS End User Messaging) for the PF Scheduling Bot.
#
# Sets up two-way SMS by subscribing the SNS topic
# (which receives inbound SMS from Pinpoint) to the bot's webhook.
#
# Prerequisites:
#   - Pinpoint/End User Messaging phone number already provisioned
#   - ALB deployed and accessible (run 04-ecs-fargate.sh first)
#
# Usage:
#   bash env_setup/06-sms.sh dev
#   WEBHOOK_URL=https://your-alb-dns/sms/webhook bash env_setup/06-sms.sh qa

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/env-config.sh" "${1:-${ENVIRONMENT:-dev}}"

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

SNS_TOPIC_NAME="${PROJECT_PREFIX}-sms-inbound-${ENVIRONMENT}"
SMS_CONFIG_SET="scheduling-agent-sms-config-${ENVIRONMENT}"

echo "SMS Setup (env=$ENVIRONMENT, region=$AWS_REGION)"
echo ""

# ── 1. SNS Topic for inbound SMS ─────────────────────────────────────
echo "▶ Creating SNS topic: $SNS_TOPIC_NAME"
TOPIC_ARN=$(aws sns create-topic \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --name "$SNS_TOPIC_NAME" \
  --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
  --query 'TopicArn' --output text)
echo "  ✓ Topic ARN: $TOPIC_ARN"
echo ""

# ── 2. Subscribe webhook to SNS topic ────────────────────────────────
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
  echo ""
  echo "  The bot will auto-confirm the subscription when SNS sends the"
  echo "  SubscriptionConfirmation request to POST /sms/webhook."
else
  echo "⚠ WEBHOOK_URL not set. Subscribe manually after ALB is deployed:"
  echo ""
  echo "  aws sns subscribe \\"
  echo "    --profile $PROFILE --region $AWS_REGION \\"
  echo "    --topic-arn $TOPIC_ARN \\"
  echo "    --protocol https \\"
  echo "    --notification-endpoint https://YOUR-ALB-DNS/sms/webhook"
fi
echo ""

# ── 3. SMS configuration set ─────────────────────────────────────────
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
echo "  SMS Setup Summary"
echo ""
echo "  SNS Topic:          $TOPIC_ARN"
echo "  Config Set:         $SMS_CONFIG_SET"
echo "  Origination Number: +18786789053 (set SMS_ORIGINATION_NUMBER env var)"
echo ""
echo "  Environment variables for ECS task:"
echo "    SMS_ORIGINATION_NUMBER=+18786789053"
echo "    SMS_CONFIGURATION_SET=$SMS_CONFIG_SET"
echo "═══════════════════════════════════════════════════════════"
