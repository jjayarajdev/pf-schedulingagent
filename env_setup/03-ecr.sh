#!/bin/bash
# Create ECR repository for the PF Scheduling Bot.
#
# Usage:
#   bash env_setup/03-ecr.sh

set -euo pipefail

PROFILE="${AWS_PROFILE:-pf-aws}"
REGION="${AWS_REGION:-us-east-1}"
REPO_NAME="pf-syn-schedulingagents-bot"

echo "Creating ECR repository: $REPO_NAME (region=$REGION)"

aws ecr create-repository \
  --profile "$PROFILE" \
  --region "$REGION" \
  --repository-name "$REPO_NAME" \
  --image-scanning-configuration scanOnPush=true \
  --tags Key=Project,Value=pf-schedulingagents-bot \
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
  --region "$REGION" \
  --repository-name "$REPO_NAME" \
  --lifecycle-policy-text "$LIFECYCLE_POLICY" \
  > /dev/null
echo "✓ Lifecycle policy set (keep last 10 images)"

ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
echo ""
echo "Repository URI: ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"
