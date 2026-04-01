#!/bin/bash
# Environment configuration for PF Scheduling Bot.
#
# Centralized VPC, subnet, and environment settings.
# Sourced by setup and deploy scripts.
#
# Usage:
#   source env_setup/env-config.sh dev    # load dev config
#   source env_setup/env-config.sh qa     # load qa config

set -euo pipefail

_ENV="${1:-${ENVIRONMENT:-dev}}"

# ── Common settings ──────────────────────────────────────────────────
export AWS_PROFILE="${AWS_PROFILE:-pf-aws}"
export PROJECT_PREFIX="pf-syn-schedulingagents"

# ── Per-environment settings ─────────────────────────────────────────
case "$_ENV" in
  dev|staging)
    export ENVIRONMENT="$_ENV"
    export AWS_REGION="${AWS_REGION:-us-east-1}"
    export VPC_ID="vpc-0169f0870d3667f01"
    export ALB_SUBNET_IDS="subnet-0528b8a28ae4bfb86,subnet-0c4ecf84ab7e1bcf8"
    export TASK_SUBNET_IDS="subnet-047f80fe65a9b48cb,subnet-0dd05e97551eff85c"
    export ECR_TAG="release-${_ENV}"
    # Dev: ALB/TG/service were recreated in PF VPC (alb2/tg2/svc)
    _SVC_SUFFIX="svc"
    _ALB_SUFFIX="alb2"
    _TG_SUFFIX="tg2"
    ;;
  qa|uat)
    export ENVIRONMENT="$_ENV"
    export AWS_REGION="${AWS_REGION:-us-east-1}"
    export VPC_ID="vpc-0fb2f3dae9b0e1c47"
    export ALB_SUBNET_IDS="subnet-0217752df72d9db9e,subnet-0cca42d20ec20a82f"
    export TASK_SUBNET_IDS="subnet-0ae7a18cb0ecbf5a2,subnet-044022f28946de60d"
    export ECR_TAG="release-${_ENV}"
    _SVC_SUFFIX="bot"
    _ALB_SUFFIX="alb"
    _TG_SUFFIX="tg"
    ;;
  prod)
    export ENVIRONMENT="prod"
    export AWS_REGION="${AWS_REGION:-us-east-2}"
    export VPC_ID="vpc-0169f0870d3667f01"
    export ALB_SUBNET_IDS=""  # TBD — need public subnet IDs for prod
    export TASK_SUBNET_IDS="subnet-053cc2fef2bbaa203,subnet-031524d9174dde42e"
    export ECR_TAG="release-prod"
    _SVC_SUFFIX="svc"
    _ALB_SUFFIX="alb"
    _TG_SUFFIX="tg"
    ;;
  *)
    echo "ERROR: Unknown environment '$_ENV'. Use: dev, staging, qa, uat, prod"
    exit 1
    ;;
esac

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
