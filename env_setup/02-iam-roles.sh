#!/bin/bash
# Create IAM roles for the PF Scheduling Bot ECS task.
#
# Roles:
#   pf-syn-schedulingagents-bot-task-role-{env}       — ECS task role (app permissions)
#   pf-syn-schedulingagents-bot-execution-role-{env}  — ECS execution role (ECR + logs)
#
# Usage:
#   bash env_setup/02-iam-roles.sh dev
#   bash env_setup/02-iam-roles.sh qa

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Load environment config ──────────────────────────────────────────
source "$SCRIPT_DIR/env-config.sh" "${1:-${ENVIRONMENT:-dev}}"

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

echo "═══════════════════════════════════════════════════════════"
echo "  PF Scheduling Bot — IAM Roles (env=${ENVIRONMENT})"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 1. ECS Task Role (application permissions) ───────────────────────
echo "▶ Creating task role: $TASK_ROLE"

# Trust policy for ECS tasks
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

aws iam create-role \
  --profile "$PROFILE" \
  --role-name "$TASK_ROLE" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
  2>/dev/null && echo "  ✓ Role created" || echo "  → Already exists"

# Inline policy: Bedrock + DynamoDB (all 4 tables) + Secrets Manager + SMS
TASK_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Bedrock",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:BatchWriteItem",
        "dynamodb:DescribeTable"
      ],
      "Resource": [
        "arn:aws:dynamodb:'"$AWS_REGION"':'"$ACCOUNT"':table/'"$PROJECT_PREFIX"'-sessions-'"$ENVIRONMENT"'",
        "arn:aws:dynamodb:'"$AWS_REGION"':'"$ACCOUNT"':table/'"$PROJECT_PREFIX"'-phone-creds-'"$ENVIRONMENT"'",
        "arn:aws:dynamodb:'"$AWS_REGION"':'"$ACCOUNT"':table/'"$PROJECT_PREFIX"'-conversations-'"$ENVIRONMENT"'",
        "arn:aws:dynamodb:'"$AWS_REGION"':'"$ACCOUNT"':table/'"$PROJECT_PREFIX"'-vapi-assistants-'"$ENVIRONMENT"'"
      ]
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:'"$AWS_REGION"':'"$ACCOUNT"':secret:vapi/api-key/'"$ENVIRONMENT"'*"
    },
    {
      "Sid": "SMS",
      "Effect": "Allow",
      "Action": "sms-voice:SendTextMessage",
      "Resource": "*"
    }
  ]
}'

aws iam put-role-policy \
  --profile "$PROFILE" \
  --role-name "$TASK_ROLE" \
  --policy-name "${TASK_ROLE}-policy" \
  --policy-document "$TASK_POLICY"
echo "  ✓ Task policy attached (Bedrock, DynamoDB x4, Secrets, SMS)"

echo ""

# ── 2. ECS Execution Role (ECR pull + CloudWatch logs) ────────────────
echo "▶ Creating execution role: $EXEC_ROLE"

aws iam create-role \
  --profile "$PROFILE" \
  --role-name "$EXEC_ROLE" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
  2>/dev/null && echo "  ✓ Role created" || echo "  → Already exists"

# Attach AWS managed policy for ECS task execution
aws iam attach-role-policy \
  --profile "$PROFILE" \
  --role-name "$EXEC_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy \
  2>/dev/null && echo "  ✓ Execution policy attached" || echo "  → Already attached"

echo ""
echo "Done. Roles:"
echo "  Task:      arn:aws:iam::${ACCOUNT}:role/${TASK_ROLE}"
echo "  Execution: arn:aws:iam::${ACCOUNT}:role/${EXEC_ROLE}"
echo ""
echo "DynamoDB tables in policy:"
echo "  - ${PROJECT_PREFIX}-sessions-${ENVIRONMENT}"
echo "  - ${PROJECT_PREFIX}-phone-creds-${ENVIRONMENT}"
echo "  - ${PROJECT_PREFIX}-conversations-${ENVIRONMENT}"
echo "  - ${PROJECT_PREFIX}-vapi-assistants-${ENVIRONMENT}"
