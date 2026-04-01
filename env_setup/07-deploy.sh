#!/bin/bash
# Build, push to ECR, and trigger ECS rolling deployment.
#
# Sources env-config.sh for region, cluster, service, and ECR tag.
# Uses environment-based ECR tags (release-dev, release-qa) — not "latest".
#
# Usage:
#   bash env_setup/07-deploy.sh dev     # deploy to dev (tag: release-dev)
#   bash env_setup/07-deploy.sh qa      # deploy to qa  (tag: release-qa)
#   bash env_setup/07-deploy.sh prod    # deploy to prod (tag: release-prod)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Load environment config ──────────────────────────────────────────
source "$SCRIPT_DIR/env-config.sh" "${1:-${ENVIRONMENT:-dev}}"

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

ECR_REPO_NAME="${PROJECT_PREFIX}-bot"
ECR_URI="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  PF Scheduling Bot — Deploy to ECS"
echo "  Environment: ${ENVIRONMENT} | Region: ${AWS_REGION}"
echo "  ECR tag: ${ECR_TAG}"
echo "═══════════════════════════════════════════════════════════"
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
echo "  Deployment triggered! (${ENVIRONMENT}, tag: ${ECR_TAG})"
echo ""
echo "  Monitor progress:"
echo "    aws ecs describe-services --profile $PROFILE --region $AWS_REGION \\"
echo "      --cluster $CLUSTER --services $SERVICE \\"
echo "      --query 'services[0].deployments' --output table"
echo ""
echo "  Tail logs:"
echo "    aws logs tail ${LOG_GROUP} \\"
echo "      --profile $PROFILE --region $AWS_REGION --follow"
echo "═══════════════════════════════════════════════════════════"
