#!/bin/bash
# Create or verify Secrets Manager entries for the PF Scheduling Bot.
#
# The Vapi API key secret may already exist from v1.2.9 at "vapi/api-key/{env}".
# This script checks and creates it only if missing.
#
# Usage:
#   bash env_setup/05-secrets.sh

set -euo pipefail

PROFILE="${AWS_PROFILE:-pf-aws}"
REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"

VAPI_SECRET_NAME="vapi/api-key/${ENV}"

echo "Checking Secrets Manager (env=$ENV, region=$REGION)"
echo ""

# ── Vapi API key ─────────────────────────────────────────────────────
echo "▶ Checking secret: $VAPI_SECRET_NAME"

SECRET_EXISTS=$(aws secretsmanager describe-secret \
  --profile "$PROFILE" \
  --region "$REGION" \
  --secret-id "$VAPI_SECRET_NAME" \
  --query 'ARN' --output text 2>/dev/null || echo "")

if [ -n "$SECRET_EXISTS" ] && [ "$SECRET_EXISTS" != "None" ]; then
  echo "  ✓ Secret exists: $SECRET_EXISTS"
  echo "  Set this in your ECS task definition:"
  echo "    VAPI_SECRET_ARN=$SECRET_EXISTS"
else
  echo "  Secret does not exist. Creating placeholder..."
  echo ""
  echo "  ⚠ You must update the secret value with your actual Vapi API key:"
  echo "    aws secretsmanager put-secret-value \\"
  echo "      --profile $PROFILE --region $REGION \\"
  echo "      --secret-id $VAPI_SECRET_NAME \\"
  echo "      --secret-string '{\"vapi_api_key\": \"YOUR_VAPI_API_KEY\"}'"
  echo ""

  ARN=$(aws secretsmanager create-secret \
    --profile "$PROFILE" \
    --region "$REGION" \
    --name "$VAPI_SECRET_NAME" \
    --description "Vapi API key for PF Scheduling Bot ($ENV)" \
    --secret-string '{"vapi_api_key": "PLACEHOLDER_UPDATE_ME"}' \
    --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
    --query 'ARN' --output text)
  echo "  ✓ Created: $ARN"
fi
echo ""
echo "Done."
