#!/bin/bash
# Build, push to ECR, and trigger ECS rolling deployment.
#
# Usage:
#   bash env_setup/07-deploy.sh              # default: dev, latest tag
#   TAG=v1.0.0 bash env_setup/07-deploy.sh   # custom image tag

set -euo pipefail

PROFILE="${AWS_PROFILE:-pf-aws}"
REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"
TAG="${TAG:-latest}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

REPO="pf-syn-schedulingagents-bot"
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"
CLUSTER="pf-syn-schedulingagents-cluster-${ENV}"
SERVICE="pf-syn-schedulingagents-bot-${ENV}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "═══════════════════════════════════════════════════════════"
echo "  PF Scheduling Bot — Deploy to ECS (env=$ENV)"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 1. ECR Login ─────────────────────────────────────────────────────
echo "▶ Logging in to ECR..."
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
echo ""

# ── 2. Build ─────────────────────────────────────────────────────────
echo "▶ Building Docker image..."
docker build --platform linux/amd64 -t "${REPO}:${TAG}" "$PROJECT_DIR"
echo ""

# ── 3. Tag + Push ────────────────────────────────────────────────────
echo "▶ Pushing to ECR: ${ECR_URI}:${TAG}"
docker tag "${REPO}:${TAG}" "${ECR_URI}:${TAG}"
docker push "${ECR_URI}:${TAG}"
echo ""

# ── 4. Force new deployment ──────────────────────────────────────────
echo "▶ Triggering ECS rolling deployment..."
aws ecs update-service \
  --profile "$PROFILE" \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --service "$SERVICE" \
  --force-new-deployment \
  --query 'service.deployments[0].{status:status,desired:desiredCount,running:runningCount}' \
  --output table

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Deployment triggered!"
echo ""
echo "  Monitor progress:"
echo "    aws ecs describe-services --profile $PROFILE --region $REGION \\"
echo "      --cluster $CLUSTER --services $SERVICE \\"
echo "      --query 'services[0].deployments' --output table"
echo ""
echo "  Tail logs:"
echo "    aws logs tail /ecs/pf-syn-schedulingagents-bot-${ENV} \\"
echo "      --profile $PROFILE --region $REGION --follow"
echo "═══════════════════════════════════════════════════════════"
