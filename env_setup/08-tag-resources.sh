#!/bin/bash
# Tag all AWS resources for cost allocation dashboard.
#
# Applies standardized tags to all 21 SchedulingAIBot resources in the
# target environment. Idempotent — safe to re-run (tagging is additive).
#
# Tags applied:
#   Project      = pf-syn
#   Application  = schedulingagents
#   Environment  = {env}
#   ManagedBy    = scripts
#
# Usage:
#   bash env_setup/08-tag-resources.sh qa
#   bash env_setup/08-tag-resources.sh prod

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ENV="${1:-}"
if [ -z "$ENV" ]; then
  echo "Usage: bash env_setup/08-tag-resources.sh <qa|prod>"
  exit 1
fi

# Load environment config (sets PROFILE, REGION, PROJECT_PREFIX, etc.)
if [ "$ENV" = "prod" ] && [ -f "$SCRIPT_DIR/env-config-prod.sh" ]; then
  source "$SCRIPT_DIR/env-config-prod.sh"
else
  source "$SCRIPT_DIR/env-config.sh" "$ENV"
fi

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
P="${PROJECT_PREFIX}"   # pf-syn-schedulingagents

# ── Tag values ───────────────────────────────────────────────────────
TAG_PROJECT="pf-syn"
TAG_APP="schedulingagents"
TAG_ENV="$ENVIRONMENT"
TAG_MANAGED="scripts"

TAGS_JSON="[{\"Key\":\"Project\",\"Value\":\"${TAG_PROJECT}\"},{\"Key\":\"Application\",\"Value\":\"${TAG_APP}\"},{\"Key\":\"Environment\",\"Value\":\"${TAG_ENV}\"},{\"Key\":\"ManagedBy\",\"Value\":\"${TAG_MANAGED}\"}]"

SUCCESS=0
FAILED=0
SKIPPED=0

tag_ok()   { echo "  ✓ $1"; SUCCESS=$((SUCCESS + 1)); }
tag_fail() { echo "  ✗ $1 — $2"; FAILED=$((FAILED + 1)); }
tag_skip() { echo "  ⊘ $1 — not found, skipping"; SKIPPED=$((SKIPPED + 1)); }

echo "═══════════════════════════════════════════════════════════"
echo "  SchedulingAIBot — Tag Resources for Cost Allocation"
echo "  Environment: ${ENVIRONMENT} | Region: ${AWS_REGION}"
echo "  Account: ${ACCOUNT}"
echo ""
echo "  Tags: Project=${TAG_PROJECT} Application=${TAG_APP}"
echo "         Environment=${TAG_ENV} ManagedBy=${TAG_MANAGED}"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 1. DynamoDB Tables (5) ───────────────────────────────────────────
echo "▶ DynamoDB Tables"

DDB_TABLES=(
  "${P}-sessions-${TAG_ENV}"
  "${P}-phone-creds-${TAG_ENV}"
  "${P}-conversations-${TAG_ENV}"
  "${P}-vapi-assistants-${TAG_ENV}"
  "${P}-outbound-calls-${TAG_ENV}"
)

for TABLE in "${DDB_TABLES[@]}"; do
  TABLE_ARN=$(aws dynamodb describe-table \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --table-name "$TABLE" \
    --query 'Table.TableArn' --output text 2>/dev/null || echo "")

  if [ -z "$TABLE_ARN" ] || [ "$TABLE_ARN" = "None" ]; then
    tag_skip "$TABLE"
    continue
  fi

  aws dynamodb tag-resource \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --resource-arn "$TABLE_ARN" \
    --tags "$TAGS_JSON" 2>/dev/null \
    && tag_ok "$TABLE" \
    || tag_fail "$TABLE" "tag-resource failed"
done
echo ""

# ── 2. ECS Cluster ───────────────────────────────────────────────────
echo "▶ ECS Cluster"

CLUSTER_ARN="arn:aws:ecs:${AWS_REGION}:${ACCOUNT}:cluster/${CLUSTER}"

aws ecs tag-resource \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --resource-arn "$CLUSTER_ARN" \
  --tags key=Project,value="$TAG_PROJECT" key=Application,value="$TAG_APP" \
         key=Environment,value="$TAG_ENV" key=ManagedBy,value="$TAG_MANAGED" 2>/dev/null \
  && tag_ok "$CLUSTER" \
  || tag_fail "$CLUSTER" "cluster may not exist"
echo ""

# ── 3. ECS Service ──────────────────────────────────────────────────
echo "▶ ECS Service"

SERVICE_ARN=$(aws ecs describe-services \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].serviceArn' --output text 2>/dev/null || echo "")

if [ -n "$SERVICE_ARN" ] && [ "$SERVICE_ARN" != "None" ]; then
  aws ecs tag-resource \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --resource-arn "$SERVICE_ARN" \
    --tags key=Project,value="$TAG_PROJECT" key=Application,value="$TAG_APP" \
           key=Environment,value="$TAG_ENV" key=ManagedBy,value="$TAG_MANAGED" 2>/dev/null \
    && tag_ok "$SERVICE" \
    || tag_fail "$SERVICE" "tag-resource failed"
else
  tag_skip "$SERVICE"
fi
echo ""

# ── 4. ECR Repository ───────────────────────────────────────────────
echo "▶ ECR Repository"

ECR_REPO_NAME="${P}-bot"
ECR_ARN="arn:aws:ecr:${AWS_REGION}:${ACCOUNT}:repository/${ECR_REPO_NAME}"

aws ecr tag-resource \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --resource-arn "$ECR_ARN" \
  --tags "$TAGS_JSON" 2>/dev/null \
  && tag_ok "$ECR_REPO_NAME" \
  || tag_fail "$ECR_REPO_NAME" "repo may not exist in this region"
echo ""

# ── 5. ALB + Target Group ───────────────────────────────────────────
echo "▶ ALB & Target Group"

ALB_ARN=$(aws elbv2 describe-load-balancers \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")

if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
  aws elbv2 add-tags \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --resource-arns "$ALB_ARN" \
    --tags Key=Project,Value="$TAG_PROJECT" Key=Application,Value="$TAG_APP" \
           Key=Environment,Value="$TAG_ENV" Key=ManagedBy,Value="$TAG_MANAGED" 2>/dev/null \
    && tag_ok "$ALB_NAME" \
    || tag_fail "$ALB_NAME" "add-tags failed"
else
  tag_skip "$ALB_NAME"
fi

TG_ARN=$(aws elbv2 describe-target-groups \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --names "$TG_NAME" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")

if [ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ]; then
  aws elbv2 add-tags \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --resource-arns "$TG_ARN" \
    --tags Key=Project,Value="$TAG_PROJECT" Key=Application,Value="$TAG_APP" \
           Key=Environment,Value="$TAG_ENV" Key=ManagedBy,Value="$TAG_MANAGED" 2>/dev/null \
    && tag_ok "$TG_NAME" \
    || tag_fail "$TG_NAME" "add-tags failed"
else
  tag_skip "$TG_NAME"
fi
echo ""

# ── 6. Security Groups ──────────────────────────────────────────────
echo "▶ Security Groups"

for SG_NAME in "${P}-alb-sg-${TAG_ENV}" "${P}-task-sg-${TAG_ENV}"; do
  SG_ID=$(aws ec2 describe-security-groups \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --filters "Name=group-name,Values=${SG_NAME}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "")

  if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
    aws ec2 create-tags \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --resources "$SG_ID" \
      --tags Key=Project,Value="$TAG_PROJECT" Key=Application,Value="$TAG_APP" \
             Key=Environment,Value="$TAG_ENV" Key=ManagedBy,Value="$TAG_MANAGED" 2>/dev/null \
      && tag_ok "$SG_NAME ($SG_ID)" \
      || tag_fail "$SG_NAME" "create-tags failed"
  else
    tag_skip "$SG_NAME"
  fi
done
echo ""

# ── 7. CloudWatch Log Group ─────────────────────────────────────────
echo "▶ CloudWatch Log Group"

aws logs tag-log-group \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --log-group-name "$LOG_GROUP" \
  --tags '{"Project":"'"$TAG_PROJECT"'","Application":"'"$TAG_APP"'","Environment":"'"$TAG_ENV"'","ManagedBy":"'"$TAG_MANAGED"'"}' 2>/dev/null \
  && tag_ok "$LOG_GROUP" \
  || tag_fail "$LOG_GROUP" "log group may not exist"
echo ""

# ── 8. SQS Queues (2) ───────────────────────────────────────────────
echo "▶ SQS Queues"

SQS_QUEUES=(
  "${P}-outbound-queue-${TAG_ENV}"
  "${P}-outbound-dlq-${TAG_ENV}"
)

for QUEUE_NAME in "${SQS_QUEUES[@]}"; do
  QUEUE_URL=$(aws sqs get-queue-url \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --queue-name "$QUEUE_NAME" \
    --query 'QueueUrl' --output text 2>/dev/null || echo "")

  if [ -n "$QUEUE_URL" ] && [ "$QUEUE_URL" != "None" ]; then
    aws sqs tag-queue \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --queue-url "$QUEUE_URL" \
      --tags Project="$TAG_PROJECT",Application="$TAG_APP",Environment="$TAG_ENV",ManagedBy="$TAG_MANAGED" 2>/dev/null \
      && tag_ok "$QUEUE_NAME" \
      || tag_fail "$QUEUE_NAME" "tag-queue failed"
  else
    tag_skip "$QUEUE_NAME"
  fi
done
echo ""

# ── 9. Secrets Manager ──────────────────────────────────────────────
echo "▶ Secrets Manager"

SECRET_NAME="vapi/api-key/${TAG_ENV}"
SECRET_ARN=$(aws secretsmanager describe-secret \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --secret-id "$SECRET_NAME" \
  --query 'ARN' --output text 2>/dev/null || echo "")

if [ -n "$SECRET_ARN" ] && [ "$SECRET_ARN" != "None" ]; then
  aws secretsmanager tag-resource \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --secret-id "$SECRET_ARN" \
    --tags "$TAGS_JSON" 2>/dev/null \
    && tag_ok "$SECRET_NAME" \
    || tag_fail "$SECRET_NAME" "tag-resource failed"
else
  tag_skip "$SECRET_NAME"
fi
echo ""

# ── 10. IAM Roles (2) ───────────────────────────────────────────────
echo "▶ IAM Roles"

IAM_ROLES=(
  "${P}-bot-task-role-${TAG_ENV}"
  "${P}-bot-execution-role-${TAG_ENV}"
)

for ROLE_NAME in "${IAM_ROLES[@]}"; do
  aws iam tag-role \
    --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --tags Key=Project,Value="$TAG_PROJECT" Key=Application,Value="$TAG_APP" \
           Key=Environment,Value="$TAG_ENV" Key=ManagedBy,Value="$TAG_MANAGED" 2>/dev/null \
    && tag_ok "$ROLE_NAME" \
    || tag_fail "$ROLE_NAME" "role may not exist"
done
echo ""

# ── 11. SNS Topic ───────────────────────────────────────────────────
echo "▶ SNS Topic"

SNS_TOPIC_NAME="${P}-sms-inbound-${TAG_ENV}"
SNS_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT}:${SNS_TOPIC_NAME}"

aws sns tag-resource \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --resource-arn "$SNS_ARN" \
  --tags Key=Project,Value="$TAG_PROJECT" Key=Application,Value="$TAG_APP" \
         Key=Environment,Value="$TAG_ENV" Key=ManagedBy,Value="$TAG_MANAGED" 2>/dev/null \
  && tag_ok "$SNS_TOPIC_NAME" \
  || tag_fail "$SNS_TOPIC_NAME" "topic may not exist"
echo ""

# ── 12. SMS Configuration Set ───────────────────────────────────────
echo "▶ SMS Configuration Set"

SMS_CONFIG="scheduling-agent-sms-config-${TAG_ENV}"
SMS_ARN=$(aws pinpoint-sms-voice-v2 describe-configuration-sets \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --configuration-set-names "$SMS_CONFIG" \
  --query 'ConfigurationSets[0].ConfigurationSetArn' --output text 2>/dev/null || echo "")

if [ -n "$SMS_ARN" ] && [ "$SMS_ARN" != "None" ]; then
  aws pinpoint-sms-voice-v2 tag-resource \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --resource-arn "$SMS_ARN" \
    --tags "$TAGS_JSON" 2>/dev/null \
    && tag_ok "$SMS_CONFIG" \
    || tag_fail "$SMS_CONFIG" "tag-resource failed"
else
  tag_skip "$SMS_CONFIG"
fi
echo ""

# ── Summary ──────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════"
echo "  Tagging Complete"
echo ""
echo "  ✓ Tagged:   ${SUCCESS}"
echo "  ✗ Failed:   ${FAILED}"
echo "  ⊘ Skipped:  ${SKIPPED}"
echo ""
echo "  Next steps:"
echo "    1. Enable cost allocation tags in AWS Billing Console:"
echo "       Billing → Cost Allocation Tags → activate Project, Application, Environment"
echo "    2. Wait 24h for tags to propagate to Cost Explorer"
echo "    3. Filter Cost Explorer by Project=pf-syn"
echo "═══════════════════════════════════════════════════════════"
