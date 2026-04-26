#!/bin/bash
# Production environment configuration for PF Scheduling Bot.
#
# Separate from env-config.sh to avoid any risk to dev/qa environments.
# Sourced by setup and deploy scripts when ENVIRONMENT=prod.
#
# Usage:
#   source env_setup/env-config-prod.sh

set -euo pipefail

# ── Common settings ──────────────────────────────────────────────────
export AWS_PROFILE="${AWS_PROFILE:-pf-aws}"
export PROJECT_PREFIX="pf-syn-schedulingagents"

# ── Prod settings (us-east-2) ────────────────────────────────────────
export ENVIRONMENT="prod"
export AWS_REGION="us-east-2"
export VPC_ID="vpc-09c6d94a64dbd2460"
export ALB_SUBNET_IDS="subnet-0ba0648672c4633b2,subnet-0caf5f8be0e4d3f67"
export TASK_SUBNET_IDS="subnet-031524d9174dde42e,subnet-053cc2fef2bbaa203"
export ECR_TAG="release-prod"
export ACM_CERT_ARN="arn:aws:acm:us-east-2:772634497954:certificate/c518df22-f048-4561-a17f-31b284b5610c"

_SVC_SUFFIX="svc"
_ALB_SUFFIX="alb"
_TG_SUFFIX="tg"

# ── Derived names ────────────────────────────────────────────────────
export CLUSTER="${PROJECT_PREFIX}-cluster-${ENVIRONMENT}"
export SERVICE="${PROJECT_PREFIX}-${_SVC_SUFFIX}-${ENVIRONMENT}"
export TASK_FAMILY="${PROJECT_PREFIX}-bot-${ENVIRONMENT}"
export ALB_NAME="${PROJECT_PREFIX}-${_ALB_SUFFIX}-${ENVIRONMENT}"
export TG_NAME="${PROJECT_PREFIX}-${_TG_SUFFIX}-${ENVIRONMENT}"
export LOG_GROUP="/ecs/${PROJECT_PREFIX}-bot-${ENVIRONMENT}"
export CONTAINER_NAME="scheduling-bot"
export ECR_REPO="${PROJECT_PREFIX}-bot"
export TASK_ROLE="${PROJECT_PREFIX}-bot-task-role-${ENVIRONMENT}"
export EXEC_ROLE="${PROJECT_PREFIX}-bot-execution-role-${ENVIRONMENT}"

echo "Environment: ${ENVIRONMENT} | Region: ${AWS_REGION} | VPC: ${VPC_ID}"
