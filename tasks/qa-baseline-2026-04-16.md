# QA Baseline — 2026-04-16 (Pre Custom LLM Deploy)

Captured before deploying Vapi Custom LLM changes. Use this to rollback if the new deployment causes issues.

## Current Deployment

| Field | Value |
|-------|-------|
| **Cluster** | `pf-syn-schedulingagents-cluster-qa` |
| **Service** | `pf-syn-schedulingagents-bot-qa` |
| **Task Definition** | `pf-syn-schedulingagents-bot-qa:8` |
| **Image** | `772634497954.dkr.ecr.us-east-1.amazonaws.com/pf-syn-schedulingagents-bot:release-qa` |
| **Image Digest** | `sha256:d5bf1c22efa3f50c7ccead335b6d62ff2d4b46774d53a885471537608220bac6` |
| **Deployed** | 2026-04-16T15:00:04 IST |
| **Status** | 1/1 running, ACTIVE |
| **Log Group** | `/ecs/pf-syn-schedulingagents-bot-qa` |

## Rollback Command

```bash
aws ecs update-service --profile pf-aws --region us-east-1 \
  --cluster pf-syn-schedulingagents-cluster-qa \
  --service pf-syn-schedulingagents-bot-qa \
  --task-definition pf-syn-schedulingagents-bot-qa:8 \
  --force-new-deployment
```

## What Changed After This Baseline

- Vapi Custom LLM endpoint (`POST /vapi/chat/completions`) replaces GPT-4o-mini for authenticated customer calls
- Customer callers now use `_build_custom_llm_assistant_config()` instead of `_build_assistant_config()`
- Store/retailer callers and outbound calls are unchanged
