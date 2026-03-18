#!/bin/bash
# Create IAM roles for the PF Scheduling Bot ECS task.
#
# Roles:
#   pf-syn-schedulingagents-bot-task-role-{env}       — ECS task role (app permissions)
#   pf-syn-schedulingagents-bot-execution-role-{env}  — ECS execution role (ECR + logs)
#
# Usage:
#   bash env_setup/02-iam-roles.sh

set -euo pipefail

PROFILE="${AWS_PROFILE:-pf-aws}"
REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

TASK_ROLE="pf-syn-schedulingagents-bot-task-role-${ENV}"
EXEC_ROLE="pf-syn-schedulingagents-bot-execution-role-${ENV}"

echo "Creating IAM roles (env=$ENV, account=$ACCOUNT)"
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
  --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
  2>/dev/null && echo "  ✓ Role created" || echo "  → Already exists"

# Inline policy: Bedrock + DynamoDB + Secrets Manager + SMS
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
        "dynamodb:DescribeTable"
      ],
      "Resource": [
        "arn:aws:dynamodb:'"$REGION"':'"$ACCOUNT"':table/pf-syn-schedulingagents-sessions-'"$ENV"'",
        "arn:aws:dynamodb:'"$REGION"':'"$ACCOUNT"':table/pf-syn-schedulingagents-phone-creds-'"$ENV"'"
      ]
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:'"$REGION"':'"$ACCOUNT"':secret:vapi/api-key/'"$ENV"'*"
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
echo "  ✓ Task policy attached"

echo ""

# ── 2. ECS Execution Role (ECR pull + CloudWatch logs) ────────────────
echo "▶ Creating execution role: $EXEC_ROLE"

aws iam create-role \
  --profile "$PROFILE" \
  --role-name "$EXEC_ROLE" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
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
