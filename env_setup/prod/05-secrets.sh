#!/bin/bash
# Create Secrets Manager entries for PRODUCTION (us-east-2).
# Copies Vapi keys from QA (us-east-1) if available, otherwise creates placeholder.
# Isolated from dev/qa — sources env-config-prod.sh only.
#
# Usage:
#   bash env_setup/prod/05-secrets.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/env-config-prod.sh"

PROFILE="${AWS_PROFILE}"
VAPI_SECRET_NAME="vapi/api-key/${ENVIRONMENT}"
QA_REGION="us-east-1"
QA_SECRET_NAME="vapi/api-key/qa"

echo "Secrets Manager Setup (PRODUCTION, region=$AWS_REGION)"
echo ""

# ── Vapi API key ─────────────────────────────────────────────────────
echo "▶ Checking secret: $VAPI_SECRET_NAME"

SECRET_EXISTS=$(aws secretsmanager describe-secret \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --secret-id "$VAPI_SECRET_NAME" \
  --query 'ARN' --output text 2>/dev/null || echo "")

if [ -n "$SECRET_EXISTS" ] && [ "$SECRET_EXISTS" != "None" ]; then
  echo "  ✓ Secret exists: $SECRET_EXISTS"
else
  # Try to copy from QA
  echo "  Secret does not exist. Checking QA for source values..."
  QA_SECRET=$(aws secretsmanager get-secret-value \
    --profile "$PROFILE" \
    --region "$QA_REGION" \
    --secret-id "$QA_SECRET_NAME" \
    --query 'SecretString' --output text 2>/dev/null || echo "")

  if [ -n "$QA_SECRET" ] && [ "$QA_SECRET" != "None" ]; then
    echo "  ✓ Found QA secret — copying to prod"
    ARN=$(aws secretsmanager create-secret \
      --profile "$PROFILE" \
      --region "$AWS_REGION" \
      --name "$VAPI_SECRET_NAME" \
      --description "Vapi API key for PF Scheduling Bot ($ENVIRONMENT)" \
      --secret-string "$QA_SECRET" \
      --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
      --query 'ARN' --output text)
    echo "  ✓ Created from QA: $ARN"
  else
    echo "  QA secret not found — creating placeholder"
    echo ""
    echo "  ⚠ Update the secret value with your actual Vapi API key:"
    echo "    aws secretsmanager put-secret-value \\"
    echo "      --profile $PROFILE --region $AWS_REGION \\"
    echo "      --secret-id $VAPI_SECRET_NAME \\"
    echo "      --secret-string '{\"vapi_api_key\": \"YOUR_KEY\", \"vapi_private_key\": \"YOUR_KEY\", \"vapi_public_key\": \"YOUR_KEY\"}'"
    echo ""

    ARN=$(aws secretsmanager create-secret \
      --profile "$PROFILE" \
      --region "$AWS_REGION" \
      --name "$VAPI_SECRET_NAME" \
      --description "Vapi API key for PF Scheduling Bot ($ENVIRONMENT)" \
      --secret-string '{"vapi_api_key": "PLACEHOLDER", "vapi_private_key": "PLACEHOLDER", "vapi_public_key": "PLACEHOLDER"}' \
      --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
      --query 'ARN' --output text)
    echo "  ✓ Created placeholder: $ARN"
  fi
fi

echo ""
echo "Done."
