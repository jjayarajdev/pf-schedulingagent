#!/bin/bash
# Create ECS cluster, task definition, service, and ALB for the PF Scheduling Bot.
#
# Sources env-config.sh for VPC, subnets, region, and naming conventions.
# ALB goes on public subnets; ECS tasks go on private subnets (assignPublicIp=DISABLED).
#
# Prerequisites:
#   - VPC with public + private subnets (env-config.sh provides these)
#   - ECR repository (run 03-ecr.sh first)
#   - IAM roles (run 02-iam-roles.sh first)
#   - Docker image pushed to ECR
#
# Usage:
#   bash env_setup/04-ecs-fargate.sh dev     # dev environment
#   bash env_setup/04-ecs-fargate.sh qa      # qa environment
#   bash env_setup/04-ecs-fargate.sh prod    # production
#
# Optional env vars:
#   ACM_CERT_ARN  — existing ACM certificate ARN (skips certificate request)
#   ACM_DOMAIN    — domain for new ACM certificate (e.g., bot-dev.projectsforce.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Load environment config ──────────────────────────────────────────
source "$SCRIPT_DIR/env-config.sh" "${1:-${ENVIRONMENT:-dev}}"

PROFILE="${AWS_PROFILE}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
ECR_REPO="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT_PREFIX}-bot"

echo "═══════════════════════════════════════════════════════════"
echo "  PF Scheduling Bot — ECS Fargate Setup"
echo "  Environment: ${ENVIRONMENT} | Region: ${AWS_REGION}"
echo "  ALB subnets (public):  ${ALB_SUBNET_IDS}"
echo "  Task subnets (private): ${TASK_SUBNET_IDS}"
echo "  ECR tag: ${ECR_TAG}"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Validate required config ─────────────────────────────────────────
if [ -z "${ALB_SUBNET_IDS:-}" ]; then
  echo "ERROR: ALB_SUBNET_IDS not set. Check env-config.sh for environment '${ENVIRONMENT}'."
  exit 1
fi
if [ -z "${TASK_SUBNET_IDS:-}" ]; then
  echo "ERROR: TASK_SUBNET_IDS not set. Check env-config.sh for environment '${ENVIRONMENT}'."
  exit 1
fi

# ── 1. CloudWatch log group ──────────────────────────────────────────
echo "▶ Creating CloudWatch log group: $LOG_GROUP"
aws logs create-log-group \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --log-group-name "$LOG_GROUP" \
  2>/dev/null && echo "  ✓ Created" || echo "  → Already exists"

aws logs put-retention-policy \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --log-group-name "$LOG_GROUP" \
  --retention-in-days 30
echo "  ✓ Retention set to 30 days"
echo ""

# ── 2. ECS Cluster ──────────────────────────────────────────────────
echo "▶ Creating ECS cluster: $CLUSTER"
aws ecs create-cluster \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --cluster-name "$CLUSTER" \
  --tags key=Project,value="${PROJECT_PREFIX}-bot" key=Environment,value="$ENVIRONMENT" \
  > /dev/null 2>&1 && echo "  ✓ Created" || echo "  → Already exists"
echo ""

# ── 3. Security Groups ──────────────────────────────────────────────
echo "▶ Creating security groups..."

# ALB security group — allows inbound 80 + 443 from internet
ALB_SG_NAME="${PROJECT_PREFIX}-alb-sg-${ENVIRONMENT}"
ALB_SG=$(aws ec2 describe-security-groups \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --filters "Name=group-name,Values=$ALB_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

if [ "$ALB_SG" = "None" ] || [ -z "$ALB_SG" ]; then
  ALB_SG=$(aws ec2 create-security-group \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --group-name "$ALB_SG_NAME" \
    --description "ALB for PF Scheduling Bot ($ENVIRONMENT)" \
    --vpc-id "$VPC_ID" \
    --query GroupId --output text)
  aws ec2 authorize-security-group-ingress \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --group-id "$ALB_SG" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 > /dev/null
  aws ec2 authorize-security-group-ingress \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --group-id "$ALB_SG" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 > /dev/null
  echo "  ✓ ALB SG created: $ALB_SG (ports 80, 443)"
else
  echo "  → ALB SG exists: $ALB_SG"
fi

# ECS task security group — allows inbound 8000 from ALB only
TASK_SG_NAME="${PROJECT_PREFIX}-task-sg-${ENVIRONMENT}"
TASK_SG=$(aws ec2 describe-security-groups \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --filters "Name=group-name,Values=$TASK_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

if [ "$TASK_SG" = "None" ] || [ -z "$TASK_SG" ]; then
  TASK_SG=$(aws ec2 create-security-group \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --group-name "$TASK_SG_NAME" \
    --description "ECS tasks for PF Scheduling Bot ($ENVIRONMENT)" \
    --vpc-id "$VPC_ID" \
    --query GroupId --output text)
  aws ec2 authorize-security-group-ingress \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --group-id "$TASK_SG" \
    --protocol tcp --port 8000 --source-group "$ALB_SG" > /dev/null
  echo "  ✓ Task SG created: $TASK_SG (port 8000 from ALB only)"
else
  echo "  → Task SG exists: $TASK_SG"
fi
echo ""

# ── 4. ACM Certificate ──────────────────────────────────────────────
# Use existing ACM_CERT_ARN if provided, else request new cert for ACM_DOMAIN,
# else look for an existing wildcard cert in the region.
if [ -z "${ACM_CERT_ARN:-}" ]; then
  if [ -n "${ACM_DOMAIN:-}" ]; then
    echo "▶ Requesting ACM certificate for: $ACM_DOMAIN"

    # Check if a cert already exists for this domain
    ACM_CERT_ARN=$(aws acm list-certificates \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --query "CertificateSummaryList[?DomainName=='${ACM_DOMAIN}'].CertificateArn | [0]" \
      --output text 2>/dev/null || echo "")

    if [ -z "$ACM_CERT_ARN" ] || [ "$ACM_CERT_ARN" = "None" ]; then
      ACM_CERT_ARN=$(aws acm request-certificate \
        --profile "$PROFILE" --region "$AWS_REGION" \
        --domain-name "$ACM_DOMAIN" \
        --validation-method DNS \
        --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
        --query CertificateArn --output text)
      echo "  ✓ Certificate requested: $ACM_CERT_ARN"
      echo "  ⚠ DNS validation required — add the CNAME record shown below to your DNS:"
      echo ""
      # Give ACM a moment to populate validation records
      sleep 3
      aws acm describe-certificate \
        --profile "$PROFILE" --region "$AWS_REGION" \
        --certificate-arn "$ACM_CERT_ARN" \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord' \
        --output table 2>/dev/null || echo "  (Run 'aws acm describe-certificate' to see validation records)"
      echo ""
      echo "  After adding the CNAME, validation typically takes 5-30 minutes."
      echo "  Re-run this script once the certificate status is ISSUED."
      echo ""
    else
      echo "  → Certificate exists: $ACM_CERT_ARN"
      CERT_STATUS=$(aws acm describe-certificate \
        --profile "$PROFILE" --region "$AWS_REGION" \
        --certificate-arn "$ACM_CERT_ARN" \
        --query 'Certificate.Status' --output text 2>/dev/null || echo "UNKNOWN")
      echo "  Status: $CERT_STATUS"
      if [ "$CERT_STATUS" != "ISSUED" ]; then
        echo "  ⚠ Certificate not yet issued. Proceeding with HTTP-only for now."
        ACM_CERT_ARN=""
      fi
    fi
  else
    echo "▶ No ACM_CERT_ARN or ACM_DOMAIN set — checking for existing wildcard cert..."
    # Look for a *.projectsforce.com cert in this region
    ACM_CERT_ARN=$(aws acm list-certificates \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --certificate-statuses ISSUED \
      --query "CertificateSummaryList[?contains(DomainName, 'projectsforce.com')].CertificateArn | [0]" \
      --output text 2>/dev/null || echo "")
    if [ -n "$ACM_CERT_ARN" ] && [ "$ACM_CERT_ARN" != "None" ]; then
      CERT_DOMAIN=$(aws acm describe-certificate \
        --profile "$PROFILE" --region "$AWS_REGION" \
        --certificate-arn "$ACM_CERT_ARN" \
        --query 'Certificate.DomainName' --output text 2>/dev/null || echo "unknown")
      echo "  → Found existing cert: $CERT_DOMAIN ($ACM_CERT_ARN)"
    else
      ACM_CERT_ARN=""
      echo "  → No suitable cert found. Will use HTTP-only."
    fi
  fi
  echo ""
fi

# ── 5. ALB + Target Group ───────────────────────────────────────────
echo "▶ Creating ALB: $ALB_NAME (public subnets)"

# Convert comma-separated to space-separated for CLI
IFS=',' read -ra ALB_SUBNET_ARRAY <<< "$ALB_SUBNET_IDS"

ALB_ARN=$(aws elbv2 describe-load-balancers \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")

if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN=$(aws elbv2 create-load-balancer \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --name "$ALB_NAME" \
    --subnets "${ALB_SUBNET_ARRAY[@]}" \
    --security-groups "$ALB_SG" \
    --scheme internet-facing \
    --type application \
    --tags Key=Project,Value="${PROJECT_PREFIX}-bot" Key=Environment,Value="$ENVIRONMENT" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  echo "  ✓ ALB created (internet-facing, public subnets)"
else
  echo "  → ALB exists"
fi

# Target group
TG_ARN=$(aws elbv2 describe-target-groups \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --names "$TG_NAME" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")

if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN=$(aws elbv2 create-target-group \
    --profile "$PROFILE" --region "$AWS_REGION" \
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

# ── Listeners ────────────────────────────────────────────────────────
if [ -n "${ACM_CERT_ARN:-}" ]; then
  # HTTPS listener (443)
  HTTPS_EXISTS=$(aws elbv2 describe-listeners \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[?Port==`443`].ListenerArn' --output text 2>/dev/null || echo "")

  if [ -z "$HTTPS_EXISTS" ] || [ "$HTTPS_EXISTS" = "None" ]; then
    aws elbv2 create-listener \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTPS --port 443 \
      --certificates CertificateArn="$ACM_CERT_ARN" \
      --default-actions Type=forward,TargetGroupArn="$TG_ARN" > /dev/null
    echo "  ✓ HTTPS listener created (port 443)"
  else
    echo "  → HTTPS listener exists"
  fi

  # HTTP→HTTPS redirect (80→443)
  HTTP_EXISTS=$(aws elbv2 describe-listeners \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[?Port==`80`].ListenerArn' --output text 2>/dev/null || echo "")

  if [ -z "$HTTP_EXISTS" ] || [ "$HTTP_EXISTS" = "None" ]; then
    aws elbv2 create-listener \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTP --port 80 \
      --default-actions Type=redirect,RedirectConfig='{Protocol=HTTPS,Port=443,StatusCode=HTTP_301}' > /dev/null
    echo "  ✓ HTTP→HTTPS redirect listener created (port 80)"
  else
    echo "  → HTTP listener exists"
  fi
else
  # No certificate — HTTP-only listener
  HTTP_EXISTS=$(aws elbv2 describe-listeners \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[?Port==`80`].ListenerArn' --output text 2>/dev/null || echo "")

  if [ -z "$HTTP_EXISTS" ] || [ "$HTTP_EXISTS" = "None" ]; then
    aws elbv2 create-listener \
      --profile "$PROFILE" --region "$AWS_REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTP --port 80 \
      --default-actions Type=forward,TargetGroupArn="$TG_ARN" > /dev/null
    echo "  ✓ HTTP listener created (port 80)"
  else
    echo "  → HTTP listener exists"
  fi
  echo "  ⚠ No ACM cert — using HTTP only. To enable HTTPS:"
  echo "    ACM_DOMAIN=bot-${ENVIRONMENT}.projectsforce.com bash env_setup/04-ecs-fargate.sh ${ENVIRONMENT}"
fi
echo ""

# ── 6. Task Definition ──────────────────────────────────────────────
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
    "image": "${ECR_REPO}:${ECR_TAG}",
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environment": [
      {"name": "ENVIRONMENT", "value": "$ENVIRONMENT"},
      {"name": "AWS_REGION", "value": "$AWS_REGION"},
      {"name": "USE_DYNAMODB_STORAGE", "value": "true"},
      {"name": "VAPI_SECRET_ARN", "value": "vapi/api-key/${ENVIRONMENT}"},
      {"name": "VAPI_PHONE_NUMBER", "value": "${VAPI_PHONE_NUMBER:-}"},
      {"name": "SMS_ORIGINATION_NUMBER", "value": "${SMS_ORIGINATION_NUMBER:-+18786789053}"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "$LOG_GROUP",
        "awslogs-region": "$AWS_REGION",
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
  --region "$AWS_REGION" \
  --cli-input-json "$TASK_DEF" > /dev/null
echo "  ✓ Task definition registered (image: ${ECR_TAG})"
echo ""

# ── 7. ECS Service ──────────────────────────────────────────────────
echo "▶ Creating ECS service: $SERVICE (private subnets, no public IP)"

SERVICE_EXISTS=$(aws ecs describe-services \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].status' --output text 2>/dev/null || echo "")

if [ "$SERVICE_EXISTS" != "ACTIVE" ]; then
  aws ecs create-service \
    --profile "$PROFILE" --region "$AWS_REGION" \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --task-definition "$TASK_FAMILY" \
    --desired-count 1 \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${TASK_SUBNET_IDS}],securityGroups=[$TASK_SG],assignPublicIp=DISABLED}" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=$CONTAINER_NAME,containerPort=8000" \
    --tags key=Project,value="${PROJECT_PREFIX}-bot" key=Environment,value="$ENVIRONMENT" \
    > /dev/null
  echo "  ✓ Service created (private subnets, assignPublicIp=DISABLED)"
else
  echo "  → Service already exists"
  echo "  To update network config, use: aws ecs update-service --network-configuration ..."
fi

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --profile "$PROFILE" --region "$AWS_REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || echo "unknown")

PROTOCOL="http"
[ -n "${ACM_CERT_ARN:-}" ] && PROTOCOL="https"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ECS Fargate setup complete!"
echo ""
echo "  Environment: ${ENVIRONMENT}"
echo "  ALB DNS:     ${ALB_DNS}"
echo "  Health:      ${PROTOCOL}://${ALB_DNS}/health"
echo "  Chat:        ${PROTOCOL}://${ALB_DNS}/chat"
echo "  Docs:        ${PROTOCOL}://${ALB_DNS}/docs"
echo "  ECR image:   ${ECR_REPO}:${ECR_TAG}"
echo ""
echo "  Network:"
echo "    VPC:            ${VPC_ID}"
echo "    ALB subnets:    ${ALB_SUBNET_IDS} (public)"
echo "    Task subnets:   ${TASK_SUBNET_IDS} (private)"
echo "    Public IP:      DISABLED (outbound via NAT Gateway)"
if [ -n "${ACM_CERT_ARN:-}" ]; then
  echo "    HTTPS:          ✓ (cert: ${ACM_CERT_ARN})"
else
  echo "    HTTPS:          ✗ (set ACM_DOMAIN to enable)"
fi
echo "═══════════════════════════════════════════════════════════"
