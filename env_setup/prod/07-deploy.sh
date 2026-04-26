#!/bin/bash
# Build, push to ECR, and trigger ECS rolling deployment for PRODUCTION (us-east-2).
# Isolated from dev/qa — sources env-config-prod.sh only.
#
# Usage:
#   bash env_setup/prod/07-deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/env-config-prod.sh"

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

ECR_REPO_NAME="${PROJECT_PREFIX}-bot"
ECR_URI="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  PF Scheduling Bot — Deploy to PRODUCTION"
echo "  Region: ${AWS_REGION}"
echo "  ECR tag: ${ECR_TAG}"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Safety check ─────────────────────────────────────────────────────
read -p "⚠ You are deploying to PRODUCTION. Continue? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "Aborted."
  exit 0
fi
echo ""

# ── 1. ECR Login ─────────────────────────────────────────────────────
echo "▶ Logging in to ECR..."
aws ecr get-login-password --profile "$PROFILE" --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo ""

# ── 2. Build ─────────────────────────────────────────────────────────
echo "▶ Building Docker image: ${ECR_REPO_NAME}:${ECR_TAG}"
docker build --platform linux/amd64 -t "${ECR_REPO_NAME}:${ECR_TAG}" "$PROJECT_DIR"
echo ""

# ── 3. Tag + Push ────────────────────────────────────────────────────
echo "▶ Pushing to ECR: ${ECR_URI}:${ECR_TAG}"
docker tag "${ECR_REPO_NAME}:${ECR_TAG}" "${ECR_URI}:${ECR_TAG}"
docker push "${ECR_URI}:${ECR_TAG}"
echo ""

# ── 4. Force new deployment ──────────────────────────────────────────
echo "▶ Triggering ECS rolling deployment..."
aws ecs update-service \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --service "$SERVICE" \
  --force-new-deployment \
  --query 'service.deployments[0].{status:status,desired:desiredCount,running:runningCount}' \
  --output table

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Production deployment triggered! (tag: ${ECR_TAG})"
echo ""
echo "  Monitor progress:"
echo "    aws ecs describe-services --profile $PROFILE --region $AWS_REGION \\"
echo "      --cluster $CLUSTER --services $SERVICE \\"
echo "      --query 'services[0].deployments' --output table"
echo ""
echo "  Tail logs:"
echo "    aws logs tail ${LOG_GROUP} \\"
echo "      --profile $PROFILE --region $AWS_REGION --follow"
echo ""
echo "  Health check:"
echo "    curl -sk https://schedulingagent.apps.projectsforce.com/health"
echo "═══════════════════════════════════════════════════════════"
