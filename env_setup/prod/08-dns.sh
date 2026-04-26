#!/bin/bash
# Create Route 53 alias record for PRODUCTION (us-east-2).
# Points schedulingagent.apps.projectsforce.com → prod ALB.
#
# Prerequisites:
#   - ALB must exist (run prod/04-ecs-fargate.sh first)
#   - Route 53 hosted zone for apps.projectsforce.com
#
# Usage:
#   bash env_setup/prod/08-dns.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/env-config-prod.sh"

PROFILE="${AWS_PROFILE}"
HOSTED_ZONE_ID="Z02311512W8PM8OG8RKCF"
RECORD_NAME="schedulingagent.apps.projectsforce.com"

echo "DNS Setup (PRODUCTION, region=$AWS_REGION)"
echo ""

# ── 1. Get ALB details ─────────────────────────────────────────────────
echo "▶ Looking up ALB: $ALB_NAME"
ALB_INFO=$(aws elbv2 describe-load-balancers \
  --profile "$PROFILE" \
  --region "$AWS_REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].{DNSName:DNSName,CanonicalHostedZoneId:CanonicalHostedZoneId}' \
  --output json 2>/dev/null)

ALB_DNS=$(echo "$ALB_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['DNSName'])")
ALB_ZONE=$(echo "$ALB_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['CanonicalHostedZoneId'])")

echo "  ALB DNS:     $ALB_DNS"
echo "  ALB Zone ID: $ALB_ZONE"
echo ""

# ── 2. Create/update Route 53 alias record ─────────────────────────────
echo "▶ Creating alias record: $RECORD_NAME → $ALB_DNS"
CHANGE_ID=$(aws route53 change-resource-record-sets \
  --profile "$PROFILE" \
  --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch "{
    \"Changes\": [{
      \"Action\": \"UPSERT\",
      \"ResourceRecordSet\": {
        \"Name\": \"$RECORD_NAME\",
        \"Type\": \"A\",
        \"AliasTarget\": {
          \"HostedZoneId\": \"$ALB_ZONE\",
          \"DNSName\": \"$ALB_DNS\",
          \"EvaluateTargetHealth\": true
        }
      }
    }]
  }" \
  --query 'ChangeInfo.Id' --output text)

echo "  ✓ Change submitted: $CHANGE_ID"
echo ""

# ── 3. Wait for propagation ────────────────────────────────────────────
echo "▶ Waiting for DNS propagation..."
aws route53 wait resource-record-sets-changed \
  --profile "$PROFILE" \
  --id "$CHANGE_ID" 2>/dev/null && echo "  ✓ Propagated" || echo "  → Check manually with: dig $RECORD_NAME"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  DNS Setup Complete (PRODUCTION)"
echo ""
echo "  Record:  $RECORD_NAME"
echo "  Target:  $ALB_DNS"
echo ""
echo "  Verify:"
echo "    dig $RECORD_NAME +short"
echo "    curl -sk https://$RECORD_NAME/health"
echo "═══════════════════════════════════════════════════════════"
