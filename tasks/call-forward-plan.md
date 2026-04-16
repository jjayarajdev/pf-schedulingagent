# Warm Call Transfer with Summary — Implementation Plan

## Goal

Replace the current "read digits aloud" support flow with a real Vapi call transfer using `warm-transfer-with-summary` mode. When a customer asks to speak to someone, the call is seamlessly transferred to the tenant's support number with an AI-generated summary of the conversation provided to the human agent before the caller is connected.

## Current State

- `_handle_support_request()` (vapi.py:927) reads the support phone number digit-by-digit via TTS
- Customer must hang up and redial manually
- `send_support_sms` action on the `ask_scheduling_bot` tool triggers this flow
- `support_number` is resolved during phone auth and cached in DynamoDB

## Target State

- Customer says "I want to speak to someone" / "transfer me" / "can I talk to a person"
- Vapi's LLM calls the built-in `transferCall` tool
- Customer hears a brief "transferring you now" message
- Vapi generates a summary of the conversation so far
- Vapi dials the tenant's `support_number`
- Human agent hears the AI-generated summary before being connected
- Calls are merged — customer and agent are talking

## Approach: Static `transferCall` with `warm-transfer-with-summary`

The `support_number` is already available at call start (from `get_or_authenticate`). We inject it into the assistant config as a `transferCall` tool destination.

## Files to Modify

| File | Change |
|------|--------|
| `src/channels/vapi.py` | Main changes — see below |

### Changes in `src/channels/vapi.py`

#### 1. `_handle_assistant_request()` (line 115)

Pass `support_number` from the auth credentials to `_build_assistant_config()`:

```python
# After line 148: client_name = creds.get("client_name", "ProjectsForce") or "ProjectsForce"
support_number = creds.get("support_number", "")

# Line 170: pass support_number
return {"assistant": _build_assistant_config(greeting, webhook_secret, support_number)}
```

#### 2. `_build_assistant_config()` (line 194)

Add `support_number` parameter and inject `transferCall` tool:

```python
def _build_assistant_config(
    first_message: str, server_secret: str = "", support_number: str = "",
) -> dict:
```

Add the `transferCall` tool to the `tools` array (alongside `ask_scheduling_bot`):

```python
# Add after the ask_scheduling_bot tool (line ~309)
{
    "type": "transferCall",
    "destinations": [
        {
            "type": "number",
            "number": f"+1{support_number}" if support_number and not support_number.startswith("+") else support_number,
            "message": "I'm transferring you to our support team now. Please stay on the line.",
            "transferPlan": {
                "mode": "warm-transfer-with-summary",
                "summaryPlan": {
                    "enabled": True,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Summarize the caller's conversation so far. Include: "
                                "who they are (name if known), what projects they discussed, "
                                "what they were trying to do, and why they need a human. "
                                "Keep it under 30 seconds of spoken text."
                            ),
                        },
                        {
                            "role": "user",
                            "content": "Transcript:\n\n{{transcript}}",
                        },
                    ],
                },
            },
        }
    ] if support_number else [],
}
```

**Note:** If `support_number` is empty, destinations is `[]` — Vapi will fire a `transfer-destination-request` webhook (future Option 2 fallback). For now this edge case means transfer won't work without a support number, which matches current behavior.

#### 3. Update system prompt rules (line 220)

Replace rule 6:

```
# OLD (line 235-236):
'6. If the user asks for a support number or to speak to someone, '
'call ask_scheduling_bot with action="send_support_sms".\n'

# NEW:
'6. If the user asks to speak to a person, transfer the call, or wants human support, '
'use the transferCall tool to connect them. Do NOT read out a phone number.\n'
```

#### 4. Remove `send_support_sms` from `ask_scheduling_bot` action enum (line 280-287)

Remove the `action` parameter entirely from the tool properties, or remove `send_support_sms` from the enum. The `ask_scheduling_bot` tool should only have `action: "ask"` (or just drop the action field since "ask" is always the default).

```python
# Remove the "action" property from ask_scheduling_bot parameters
# Lines 280-288: delete the action enum
# Line 272: remove "action" reference
```

#### 5. Remove `_handle_support_request()` (line 927-964) and `_digit_word()` (line 967-971)

These are no longer needed — Vapi handles the transfer natively.

#### 6. Remove support request routing in webhook handler (line 687-688)

```python
# Remove:
if action == "send_support_sms":
    return _handle_support_request(user_id, tool_call_id)
```

#### 7. Store assistant config — same changes

Apply the same `transferCall` tool addition to `_build_store_assistant_config()` if store callers should also be able to transfer. The store auth flow also resolves `support_number` via `authenticate_store()`.

## Tests to Update

| File | Change |
|------|--------|
| `tests/unit/test_vapi.py` | Remove/update `send_support_sms` tests, add `transferCall` config tests |

- Test that `_build_assistant_config(greeting, secret, "3157613122")` includes a `transferCall` tool with the correct E.164 number
- Test that empty `support_number` produces empty destinations array
- Test that `send_support_sms` action no longer exists in the tool config
- Remove tests for `_handle_support_request` and `_digit_word`

## Verification

1. `uv run pytest` — all tests pass
2. Deploy to ECS dev
3. Call in → ask "can I speak to someone?" → hear "transferring you now" → support phone rings → agent hears summary → calls merge
4. Verify CloudWatch logs show transfer event
5. Test edge case: call when support_number is empty → graceful fallback (bot says it can't transfer right now)

## Rollback

If warm transfer doesn't work (telephony provider issues), revert to the `send_support_sms` approach by reverting this branch. The `_handle_support_request` code is preserved in git history.

## Future Enhancements

- **Option 2 (dynamic)**: Handle `transfer-destination-request` webhook for runtime routing (business hours, on-call rotation)
- **`warm-transfer-experimental`**: Full assistant-based transfer with hold music, voicemail detection, and transfer-cancel fallback
- **Store caller transfers**: Wire up store assistant config with same transferCall pattern
