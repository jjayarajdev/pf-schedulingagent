# Fix Customer Call Issues — Deep Analysis & Plan

## How the system works today

```
INBOUND CALL:
  Customer → Vapi STT → GPT-4o → ask_scheduling_bot → Our Claude → tools → response → GPT speaks

RESCHEDULE FLOW (current):
  1. Customer: "reschedule"
  2. Claude calls reschedule_appointment(project_id)
  3. Tool hits PF API: GET /cancel-reschedule (ATOMIC: cancels old + returns new dates)
  4. ⚠️ Old appointment is NOW GONE
  5. Claude shows dates → customer picks date → get_time_slots → picks time
  6. confirm_appointment books the new one
  7. If step 5 or 6 FAILS → customer has NO appointment
```

---

## Issue 1 (P0): Reschedule Cancels Appointment, Then Fails

### What happens
`reschedule_appointment` calls PF's `/cancel-reschedule` endpoint which atomically cancels the existing appointment AND returns new dates. The cancel happens IMMEDIATELY in step 3. If anything fails after that (date fetch for Doors, GPT confusion, customer hangs up), the old appointment is gone.

Call 24: Doors scheduled for May 1 → reschedule → cancel succeeded → date fetch FAILED → customer has nothing.
Call 20: Windows scheduled → reschedule → cancel + dates → user picks Apr 30 → confirm fails ("already scheduled" race condition) → cancel retried → dates fail → customer has nothing.

### Root cause chain
1. `reschedule_appointment` cancels FIRST, returns dates SECOND
2. The cancel is irreversible — there's no "undo cancel" API
3. Doors project date fetch consistently fails (Issue 2)
4. Combined: cancel + failed fetch = data loss

### Fix: Two-phase reschedule — don't cancel until new date is confirmed

**The key insight:** `get_available_dates` does NOT check `ProjectStatusRules.can_schedule()`. It goes straight to the PF API. For "Scheduled" projects, the API returns HTTP 400 with "already scheduled" — our tool catches this and returns `{already_scheduled: true}`. So we CAN'T use `get_available_dates` for a scheduled project without cancelling first.

**BUT** — we can change the flow so the cancel happens at the LAST possible moment:

**Change A — New prompt flow:**
```
## Reschedule Flow (SAFE — old appointment preserved until new one is booked)
1. Clarify intent (reschedule vs cancel)
2. reschedule_appointment(project_id) — cancels old + returns new dates
3. Customer picks date → get_time_slots → picks time
4. Ask for confirmation
5. confirm_appointment — books the new date
6. ⚠️ If step 3-5 fails, LOG the old appointment details for recovery
```

The flow stays the same (we can't avoid the cancel because the PF API doesn't return dates for scheduled projects). But we add a **safety net**:

**Change B — Cache old appointment before cancel:**
In `reschedule_appointment`, BEFORE calling the API:
```python
# Save old appointment details for recovery if reschedule is incomplete
old_appointment = _get_cached_appointment(project_id)  # date, time from project cache
_reschedule_old_appointment[project_id] = old_appointment
```

**Change C — End-of-call incomplete reschedule detection:**
In the Vapi `end-of-call-report` handler, check:
```python
if project_id in _reschedule_pending:
    # Reschedule started but never completed — customer lost their appointment
    logger.error(
        "INCOMPLETE RESCHEDULE: project=%s old_appointment=%s — "
        "appointment was cancelled but new one was never booked",
        project_id, _reschedule_old_appointment.get(project_id),
    )
    # Could also: auto-rebook the old appointment, or create a support ticket
```

**Change D — Retry before giving up:**
In the prompt, add:
```
If date fetch fails during reschedule, try get_available_dates one more time.
If it fails again, tell the customer: "I wasn't able to pull up new dates,
but your original appointment has been cancelled. Let me transfer you to
the office so they can help rebook." — then transfer.
NEVER just say "try again later" and hang up.
```

### Files changed
- `src/tools/scheduling.py` — cache old appointment in `reschedule_appointment`, add `_reschedule_old_appointment` dict
- `src/channels/vapi.py` — detect incomplete reschedule in end-of-call handler
- `src/orchestrator/prompts/scheduling_agent.py` — retry + transfer on failure

---

## Issue 2 (P0): Doors Project Date Fetch Consistently Fails

### What happens
`get_available_dates` and `get_time_slots` fail consistently for the Doors project type. Windows, Fence, Millwork all work. 5 calls affected.

### Root cause (suspected)
Unknown without seeing the actual API response. Could be:
- Doors project has a different configuration in PF (no technicians assigned to that project type)
- The API endpoint returns an error or empty data for Doors
- Date range doesn't overlap with Doors availability window

### Fix: Better logging + graceful handling

**Change A — Log full API request/response for failures:**
In `get_available_dates` and `get_time_slots`, when the API returns empty dates or an error, log the FULL request URL and response body:
```python
except httpx.HTTPError as exc:
    logger.exception(
        "Failed to get available dates: project=%s url=%s status=%s body=%s",
        project_id, url, getattr(exc.response, 'status_code', 'N/A'),
        getattr(exc.response, 'text', 'N/A')[:500],
    )
```

Also log when dates are empty (not an exception but still a failure):
```python
if not available_dates:
    logger.warning(
        "No dates returned: project=%s project_type=%s url=%s response_keys=%s",
        project_id, project.get("projectType"), url, list(data.keys()),
    )
```

**Change B — Retry with expanded date range:**
Already implemented (10 days → 21 days). But add a third retry with 30 days for the specific failure case.

**Change C — Don't hide the error:**
Current response: "Sorry, I couldn't check available dates right now. Please try again later."
Better: Include the project type so we can correlate in logs: "I'm unable to find available dates for your Doors project. Let me transfer you to the office — they can check the schedule directly."

### Files changed
- `src/tools/scheduling.py` — enhanced logging in `get_available_dates` and `get_time_slots`

---

## Issue 5 (P1): Verbose Date Listing on Voice

### What happens
Claude returns all 9 dates with weather details. GPT reads them one-by-one: "Monday April 20 first, moderate drizzle 70 degrees. Tuesday April 20 second, overcast 69 degrees..." — 30+ seconds of listing that customers can't process aurally.

### Root cause
The scheduling agent prompt has no voice-specific date summarization rule. It returns the same format for chat and voice.

### Fix: Voice-aware date summarization in scheduling agent prompt

**Change:** Add to scheduling agent prompt:
```
## CRITICAL: Date Presentation for Voice Channel
When the channel is voice/phone (channel="vapi"), NEVER list all dates individually.
Instead, summarize:
- "I have dates available next week Monday through Thursday, and the week after
  Monday through Friday. Do you have a preference for which week?"
- After customer picks a week or range, narrow to specific dates
- Only mention weather if the customer asks about it, or if there's rain/snow
  on the date they pick
- Keep it to 2-3 sentences maximum

For chat channel, you can list all dates with details — the UI renders them as cards.
```

The scheduling agent already receives `channel: "vapi"` in `additional_params`. We need it to check this in the prompt response.

### Files changed
- `src/orchestrator/prompts/scheduling_agent.py` — add voice date summarization rule

---

## Issue 6 (P1): Outbound Calls for Already-Scheduled Projects

### What happens
3 of 6 outbound calls were for projects that were already scheduled. `_prefetch_project_data()` calls `get_available_dates()` which returns `{already_scheduled: true, available_dates: []}`. The call proceeds anyway. The AI then says "your appointment is already scheduled" but can't tell the customer the date or time.

### Root cause
`_prefetch_project_data()` doesn't check the `already_scheduled` flag. The call is placed regardless.

### Fix: Gate outbound calls on scheduling status

**Change A — Check after prefetch:**
In `outbound_consumer.py`, after `_prefetch_project_data()`:
```python
prefetched = await _prefetch_project_data(...)
dates_data = prefetched.get("dates", {})

if dates_data.get("already_scheduled"):
    logger.info(
        "Skipping outbound call — project %s is already scheduled",
        project_id,
    )
    return  # Don't place the call

if not dates_data.get("available_dates"):
    logger.warning(
        "Skipping outbound call — no dates available for project %s",
        project_id,
    )
    return  # Don't place the call
```

This also fixes **Issue 10 (Outbound no-dates)** — if prefetch returns empty dates, don't waste the call.

**Change B — Check project status from prefetched data:**
```python
project = prefetched.get("project", {})
status = (project.get("status") or "").lower()
if status in ("completed", "cancelled", "closed", "scheduled"):
    logger.info("Skipping outbound call — project %s status: %s", project_id, status)
    return
```

### Files changed
- `src/channels/outbound_consumer.py` — add status/dates checks before `create_outbound_call()`

---

## Issue 7 (P1): Tool Call Latency (10.3s avg)

### What happens
`ask_scheduling_bot` takes 10.3s on average (max 31.6s). Customer hears silence or a single "One moment."

### Root cause
Two-LLM architecture overhead:
1. GPT formats the tool call → webhook to our server (~200ms)
2. Our server routes through orchestrator → Bedrock classifier (~1s)
3. Claude processes the request + calls PF API tools (~5-8s)
4. Response back through orchestrator → webhook response → GPT (~1s)

### Fix: No code change — this is architectural

The **Custom LLM plan** (already in `tasks/`) eliminates GPT from the chain entirely. That removes ~2-3s of overhead and simplifies the flow.

For now, the "One moment." filler is the only mitigation. The latency will drop significantly once Custom LLM is implemented.

**No changes for this issue** — tracked in Custom LLM plan.

---

## Issue 8 (P1): Date Mispronunciation ("April 20 first")

### What happens
TTS reads "21st" as "twenty first" but when preceded by "April 20" it sounds like "April twenty... first" → "April 20 first". The customer hears "April 20 first" and says it back. Claude then interprets "April 20 first" as April 20th (which is in the past), creating an infinite confusion loop (Call 17).

### Root cause
Claude returns dates like "April 21st" and GPT passes them to TTS. The TTS engine (Cartesia) reads "21st" in a way that sounds like "20 first" when preceded by "April".

### Fix: Spell out dates as full words in voice responses

**Change A — Scheduling agent prompt rule:**
```
## CRITICAL: Date Format for Voice Channel
When the channel is voice/phone, ALWAYS write dates as full words:
- "Monday, April twenty-first" NOT "Monday, April 21st"
- "Tuesday, April twenty-second" NOT "Tuesday, April 22nd"
- "Friday, May first" NOT "Friday, May 1st"
This prevents TTS mispronunciation.
```

**Change B — `format_for_voice()` post-processing:**
Add a date formatting step in `format_for_voice()` that converts ordinal suffixes to words:
```python
# Convert ordinal dates to words for TTS clarity
text = re.sub(r'\b21st\b', 'twenty-first', text)
text = re.sub(r'\b22nd\b', 'twenty-second', text)
text = re.sub(r'\b23rd\b', 'twenty-third', text)
text = re.sub(r'\b(\d+)th\b', _ordinal_to_words, text)  # general handler
```

This is defense in depth — the prompt tells Claude to use words, and `format_for_voice` catches any that slip through.

### Files changed
- `src/orchestrator/prompts/scheduling_agent.py` — voice date format rule
- `src/channels/formatters.py` — ordinal-to-words in `format_for_voice()`

---

## Issue 11 (P2): Wrong Project During Reschedule

### What happens
Call 17: User says "schedule window delivery project" but Claude tries to reschedule the Doors project instead.

### Root cause
GPT passes the user's words to `ask_scheduling_bot`. Claude may lose track of which project is being discussed, especially in long calls with multiple projects.

### Fix: Prompt reinforcement

**Change:** Add to scheduling agent prompt:
```
## CRITICAL: Project Continuity During Multi-Step Flows
When the customer is in a scheduling/reschedule/cancel flow, ALWAYS confirm
which project you're acting on by including the project type in your response:
- "Your WINDOWS DELIVERY is set for April 22nd. Should I confirm?"
- NOT "Your appointment is set for April 22nd."
If the customer switches projects mid-flow, acknowledge: "Switching to your
Doors project. Let me check available dates for that one."
```

### Files changed
- `src/orchestrator/prompts/scheduling_agent.py`

---

## Issue 12 (P2): "thousand 26" Instead of "2026"

### What happens
TTS reads "2026" as "thousand twenty-six" or garbles it in some contexts.

### Fix: Year formatting in `format_for_voice()`

**Change:** Add year substitution:
```python
# Replace years with spoken form for TTS
text = text.replace("2026", "twenty twenty-six")
text = text.replace("2027", "twenty twenty-seven")
```

Simple string replacement — 2026 only appears as a year in our responses.

### Files changed
- `src/channels/formatters.py` — year replacement in `format_for_voice()`

---

## Summary: All Changes by File

| File | Issues Fixed | Changes |
|------|-------------|---------|
| `src/orchestrator/prompts/scheduling_agent.py` | 1, 5, 8, 11 | Reschedule retry+transfer rule, voice date summarization, ordinal date words, project continuity |
| `src/tools/scheduling.py` | 1, 2 | Cache old appointment before reschedule, enhanced failure logging |
| `src/channels/outbound_consumer.py` | 6, 10 | Skip calls for already-scheduled or no-dates projects |
| `src/channels/formatters.py` | 8, 12 | Ordinal-to-words conversion, year formatting in `format_for_voice()` |
| `src/channels/vapi.py` | 1 | Detect incomplete reschedule in end-of-call handler |

## What each fix actually prevents

| Fix | Before | After |
|-----|--------|-------|
| **Reschedule safety net** | Customer loses appointment silently | Logged for recovery, agent offers transfer instead of "try again later" |
| **Doors logging** | "Failed to get dates" — no idea why | Full API request/response logged, correlatable by project type |
| **Voice date summary** | 30-second date dump with weather | "I have dates next week and the week after. Any preference?" |
| **Outbound gating** | Calls already-scheduled projects, wastes money | Skips them, saves $0.20/call |
| **Date pronunciation** | "April 20 first" → infinite loop | "Monday, April twenty-first" → clear |
| **Project continuity** | Reschedules wrong project | Always names the project being acted on |
| **Year formatting** | "thousand 26" | "twenty twenty-six" |

## Implementation Order

1. **Issue 8 + 12** (formatters.py) — quickest, biggest voice quality improvement
2. **Issue 5** (date summarization prompt) — huge voice UX improvement
3. **Issue 6** (outbound gating) — saves wasted calls immediately
4. **Issue 2** (Doors logging) — need this to diagnose the root cause
5. **Issue 1** (reschedule safety net) — needs careful implementation
6. **Issue 11** (project continuity prompt) — minor prompt addition
7. **Issue 7** (latency) — deferred to Custom LLM plan
