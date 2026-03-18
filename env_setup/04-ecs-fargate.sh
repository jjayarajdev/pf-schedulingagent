#!/bin/bash
# Create ECS cluster, task definition, service, and ALB for the PF Scheduling Bot.
#
# Prerequisites:
#   - VPC with public subnets (reuse ai-support VPC or provide via env vars)
#   - ECR repository (run 03-ecr.sh first)
#   - IAM roles (run 02-iam-roles.sh first)
#   - Docker image pushed to ECR
#
# Usage:
#   bash env_setup/04-ecs-fargate.sh
#
# Required env vars (or will prompt):
#   VPC_ID, SUBNET_IDS (comma-separated), ACM_CERT_ARN (for HTTPS)

set -euo pipefail

PROFILE="${AWS_PROFILE:-pf-aws}"
REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

CLUSTER="pf-syn-schedulingagents-cluster-${ENV}"
SERVICE="pf-syn-schedulingagents-bot-${ENV}"
TASK_FAMILY="pf-syn-schedulingagents-bot-${ENV}"
ALB_NAME="pf-syn-schedulingagents-alb-${ENV}"
TG_NAME="pf-syn-schedulingagents-tg-${ENV}"
LOG_GROUP="/ecs/pf-syn-schedulingagents-bot-${ENV}"
CONTAINER_NAME="scheduling-bot"
ECR_REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/pf-syn-schedulingagents-bot"
TASK_ROLE="pf-syn-schedulingagents-bot-task-role-${ENV}"
EXEC_ROLE="pf-syn-schedulingagents-bot-execution-role-${ENV}"

echo "═══════════════════════════════════════════════════════════"
echo "  PF Scheduling Bot — ECS Fargate Setup (env=$ENV)"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Check required env vars
if [ -z "${VPC_ID:-}" ] || [ -z "${SUBNET_IDS:-}" ]; then
  echo "ERROR: Set VPC_ID and SUBNET_IDS (comma-separated) env vars."
  echo ""
  echo "  Example:"
  echo "    VPC_ID=vpc-0abc123 SUBNET_IDS=subnet-1,subnet-2 bash env_setup/04-ecs-fargate.sh"
  echo ""
  echo "  To find your VPC:"
  echo "    aws ec2 describe-vpcs --profile $PROFILE --region $REGION --query 'Vpcs[*].[VpcId,Tags[?Key==\`Name\`].Value|[0]]' --output table"
  exit 1
fi

# ── 1. CloudWatch log group ──────────────────────────────────────────
echo "▶ Creating CloudWatch log group: $LOG_GROUP"
aws logs create-log-group \
  --profile "$PROFILE" \
  --region "$REGION" \
  --log-group-name "$LOG_GROUP" \
  2>/dev/null && echo "  ✓ Created" || echo "  → Already exists"

aws logs put-retention-policy \
  --profile "$PROFILE" \
  --region "$REGION" \
  --log-group-name "$LOG_GROUP" \
  --retention-in-days 30
echo "  ✓ Retention set to 30 days"
echo ""

# ── 2. ECS Cluster ──────────────────────────────────────────────────
echo "▶ Creating ECS cluster: $CLUSTER"
aws ecs create-cluster \
  --profile "$PROFILE" \
  --region "$REGION" \
  --cluster-name "$CLUSTER" \
  --tags key=Project,value=pf-schedulingagents-bot key=Environment,value="$ENV" \
  > /dev/null 2>&1 && echo "  ✓ Created" || echo "  → Already exists"
echo ""

# ── 3. Security Groups ──────────────────────────────────────────────
echo "▶ Creating security groups..."

# ALB security group
ALB_SG_NAME="pf-syn-schedulingagents-alb-sg-${ENV}"
ALB_SG=$(aws ec2 describe-security-groups \
  --profile "$PROFILE" --region "$REGION" \
  --filters "Name=group-name,Values=$ALB_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

if [ "$ALB_SG" = "None" ] || [ -z "$ALB_SG" ]; then
  ALB_SG=$(aws ec2 create-security-group \
    --profile "$PROFILE" --region "$REGION" \
    --group-name "$ALB_SG_NAME" \
    --description "ALB for PF Scheduling Bot ($ENV)" \
    --vpc-id "$VPC_ID" \
    --query GroupId --output text)
  aws ec2 authorize-security-group-ingress \
    --profile "$PROFILE" --region "$REGION" \
    --group-id "$ALB_SG" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 > /dev/null
  aws ec2 authorize-security-group-ingress \
    --profile "$PROFILE" --region "$REGION" \
    --group-id "$ALB_SG" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 > /dev/null
  echo "  ✓ ALB SG created: $ALB_SG"
else
  echo "  → ALB SG exists: $ALB_SG"
fi

# ECS task security group
TASK_SG_NAME="pf-syn-schedulingagents-task-sg-${ENV}"
TASK_SG=$(aws ec2 describe-security-groups \
  --profile "$PROFILE" --region "$REGION" \
  --filters "Name=group-name,Values=$TASK_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

if [ "$TASK_SG" = "None" ] || [ -z "$TASK_SG" ]; then
  TASK_SG=$(aws ec2 create-security-group \
    --profile "$PROFILE" --region "$REGION" \
    --group-name "$TASK_SG_NAME" \
    --description "ECS tasks for PF Scheduling Bot ($ENV)" \
    --vpc-id "$VPC_ID" \
    --query GroupId --output text)
  aws ec2 authorize-security-group-ingress \
    --profile "$PROFILE" --region "$REGION" \
    --group-id "$TASK_SG" \
    --protocol tcp --port 8000 --source-group "$ALB_SG" > /dev/null
  echo "  ✓ Task SG created: $TASK_SG"
else
  echo "  → Task SG exists: $TASK_SG"
fi
echo ""

# ── 4. ALB + Target Group ───────────────────────────────────────────
echo "▶ Creating ALB: $ALB_NAME"

# Convert comma-separated to space-separated for CLI
IFS=',' read -ra SUBNET_ARRAY <<< "$SUBNET_IDS"

ALB_ARN=$(aws elbv2 describe-load-balancers \
  --profile "$PROFILE" --region "$REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")

if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN=$(aws elbv2 create-load-balancer \
    --profile "$PROFILE" --region "$REGION" \
    --name "$ALB_NAME" \
    --subnets "${SUBNET_ARRAY[@]}" \
    --security-groups "$ALB_SG" \
    --scheme internet-facing \
    --type application \
    --tags Key=Project,Value=pf-schedulingagents-bot Key=Environment,Value="$ENV" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  echo "  ✓ ALB created"
else
  echo "  → ALB exists"
fi

# Target group
TG_ARN=$(aws elbv2 describe-target-groups \
  --profile "$PROFILE" --region "$REGION" \
  --names "$TG_NAME" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")

if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN=$(aws elbv2 create-target-group \
    --profile "$PROFILE" --region "$REGION" \
    --name "$TG_NAME" \
    --protocol HTTP --port 8000 \
    --vpc-id "$VPC_ID" \
    --target-type ip \
    --health-check-path /health \
    --health-check-interval-seconds 30 \
    --healthy-threshold-count 2 \
    --unhealthy-threshold-count 3 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
  echo "  ✓ Target group created"
else
  echo "  → Target group exists"
fi

# HTTPS listener (if cert ARN provided)
if [ -n "${ACM_CERT_ARN:-}" ]; then
  LISTENER_EXISTS=$(aws elbv2 describe-listeners \
    --profile "$PROFILE" --region "$REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[?Port==`443`].ListenerArn' --output text 2>/dev/null || echo "")

  if [ -z "$LISTENER_EXISTS" ] || [ "$LISTENER_EXISTS" = "None" ]; then
    aws elbv2 create-listener \
      --profile "$PROFILE" --region "$REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTPS --port 443 \
      --certificates CertificateArn="$ACM_CERT_ARN" \
      --default-actions Type=forward,TargetGroupArn="$TG_ARN" > /dev/null
    echo "  ✓ HTTPS listener created"
  else
    echo "  → HTTPS listener exists"
  fi

  # HTTP→HTTPS redirect
  HTTP_EXISTS=$(aws elbv2 describe-listeners \
    --profile "$PROFILE" --region "$REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[?Port==`80`].ListenerArn' --output text 2>/dev/null || echo "")

  if [ -z "$HTTP_EXISTS" ] || [ "$HTTP_EXISTS" = "None" ]; then
    aws elbv2 create-listener \
      --profile "$PROFILE" --region "$REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTP --port 80 \
      --default-actions Type=redirect,RedirectConfig='{Protocol=HTTPS,Port=443,StatusCode=HTTP_301}' > /dev/null
    echo "  ✓ HTTP→HTTPS redirect listener created"
  fi
else
  # No cert — create HTTP-only listener so ALB forwards to target group
  HTTP_EXISTS=$(aws elbv2 describe-listeners \
    --profile "$PROFILE" --region "$REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[?Port==`80`].ListenerArn' --output text 2>/dev/null || echo "")

  if [ -z "$HTTP_EXISTS" ] || [ "$HTTP_EXISTS" = "None" ]; then
    aws elbv2 create-listener \
      --profile "$PROFILE" --region "$REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTP --port 80 \
      --default-actions Type=forward,TargetGroupArn="$TG_ARN" > /dev/null
    echo "  ✓ HTTP listener created (port 80)"
  else
    echo "  → HTTP listener exists"
  fi
  echo "  ⚠ No ACM_CERT_ARN set — using HTTP only. Set it to enable HTTPS."
fi
echo ""

# ── 5. Task Definition ──────────────────────────────────────────────
echo "▶ Registering task definition: $TASK_FAMILY"

TASK_DEF=$(cat <<TASKEOF
{
  "family": "$TASK_FAMILY",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT}:role/${EXEC_ROLE}",
  "taskRoleArn": "arn:aws:iam::${ACCOUNT}:role/${TASK_ROLE}",
  "containerDefinitions": [{
    "name": "$CONTAINER_NAME",
    "image": "${ECR_REPO}:latest",
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environment": [
      {"name": "ENVIRONMENT", "value": "$ENV"},
      {"name": "AWS_REGION", "value": "$REGION"},
      {"name": "USE_DYNAMODB_STORAGE", "value": "true"},
      {"name": "VAPI_SECRET_ARN", "value": "vapi/api-key/${ENV}"},
      {"name": "VAPI_PHONE_NUMBER", "value": "${VAPI_PHONE_NUMBER:-}"},
      {"name": "SMS_ORIGINATION_NUMBER", "value": "+18786789053"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "$LOG_GROUP",
        "awslogs-region": "$REGION",
        "awslogs-stream-prefix": "ecs"
      }
    },
    "essential": true
  }]
}
TASKEOF
)

aws ecs register-task-definition \
  --profile "$PROFILE" \
  --region "$REGION" \
  --cli-input-json "$TASK_DEF" > /dev/null
echo "  ✓ Task definition registered"
echo ""

# ── 6. ECS Service ──────────────────────────────────────────────────
echo "▶ Creating ECS service: $SERVICE"

SERVICE_EXISTS=$(aws ecs describe-services \
  --profile "$PROFILE" --region "$REGION" \
  --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].status' --output text 2>/dev/null || echo "")

if [ "$SERVICE_EXISTS" != "ACTIVE" ]; then
  aws ecs create-service \
    --profile "$PROFILE" --region "$REGION" \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --task-definition "$TASK_FAMILY" \
    --desired-count 1 \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=$CONTAINER_NAME,containerPort=8000" \
    --tags key=Project,value=pf-schedulingagents-bot key=Environment,value="$ENV" \
    > /dev/null
  echo "  ✓ Service created"
else
  echo "  → Service already exists"
fi

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --profile "$PROFILE" --region "$REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || echo "unknown")

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ECS Fargate setup complete!"
echo ""
echo "  ALB DNS:  $ALB_DNS"
echo "  Health:   https://$ALB_DNS/health"
echo "  Chat:     https://$ALB_DNS/chat"
echo "  Docs:     https://$ALB_DNS/docs"
echo "═══════════════════════════════════════════════════════════"
