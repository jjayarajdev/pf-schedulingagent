#!/bin/bash
# Create ECR repository for PRODUCTION (us-east-2).
# Isolated from dev/qa — sources env-config-prod.sh only.
#
# ECR is region-specific — prod (us-east-2) needs its own repo.
#
# Usage:
#   bash env_setup/prod/03-ecr.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/env-config-prod.sh"

PROFILE="${AWS_PROFILE}"
REPO_NAME="${PROJECT_PREFIX}-bot"

echo "Creating ECR repository: $REPO_NAME (region=$AWS_REGION)"

aws ecr create-repository \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --repository-name "$REPO_NAME" \
  --image-scanning-configuration scanOnPush=true \
  --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
  2>/dev/null && echo "✓ Repository created" || echo "→ Already exists"

# Set lifecycle policy: keep last 10 images
LIFECYCLE_POLICY='{
  "rules": [{
    "rulePriority": 1,
    "description": "Keep last 10 images",
    "selection": {
      "tagStatus": "any",
      "countType": "imageCountMoreThan",
      "countNumber": 10
    },
    "action": { "type": "expire" }
  }]
}'

aws ecr put-lifecycle-policy \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --repository-name "$REPO_NAME" \
  --lifecycle-policy-text "$LIFECYCLE_POLICY" \
  > /dev/null
echo "✓ Lifecycle policy set (keep last 10 images)"

ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
echo ""
echo "Repository URI: ${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}"
