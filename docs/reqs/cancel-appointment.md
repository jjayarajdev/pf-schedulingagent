# Cancel Appointment — Standalone Cancel Flow

## Context

Currently the bot treats **all cancellation requests as reschedules** — when a customer says "cancel", the bot calls `cancel-reschedule` which cancels the existing appointment and immediately offers new dates. There is no standalone cancel path.

The PF CX Portal has a separate **cancel-appointment** API that performs a pure cancellation without triggering a reschedule flow.

## Current Bot Behavior

- Customer says "cancel" → bot calls `reschedule_appointment` → cancels existing + fetches new dates
- System prompt enforces: "Cancel and reschedule mean the same thing"
- No way for a customer to just cancel without being offered new dates

## PF Cancel-Appointment API

**Endpoint:** `GET /scheduler/{client_id}/{project_id}/cancel-appointment`

**Method:** GET (no request body — reason cannot be passed to the API itself)

**Auth:** Bearer token + `client_id` header

**Response (200 OK):**
```json
{
    "data": {
        "message": "Appointment cancelled successfully"
    }
}
```

**Tested on QA:** 2026-04-03, project 90000119 (Windows Installation), client 16PF11PF

### URL Pattern Difference

| API | URL Pattern | Method |
|-----|-------------|--------|
| cancel-appointment (CX Portal) | `/scheduler/{client_id}/{project_id}/cancel-appointment` | GET |
| cancel-reschedule (bot current) | `/scheduler/client/{client_id}/project/{project_id}/cancel-reschedule` | GET |
| schedule (bot current) | `/scheduler/client/{client_id}/project/{project_id}/schedule` | POST |

Note: cancel-appointment uses a different URL pattern (no `client/` and `project/` path prefixes) compared to the other scheduler APIs.

## Proposed Changes (Not Yet Implemented)

### 1. Add `cancel_appointment` as a Separate Tool

New tool that calls the `/cancel-appointment` endpoint for a pure cancel without rescheduling.

**Flow:**
1. Call `GET /scheduler/{client_id}/{project_id}/cancel-appointment`
2. On success, ask the customer for a cancellation reason
3. Post a note to the project via `/project-notes/add-note` with the reason:
   - `"CANCELLATION: {reason provided by customer}"`
   - If no reason given: `"Appointment cancelled by customer via AI Scheduling Assistant"`

### 2. Update Prompt — Cancel vs Reschedule

Remove the "cancel = reschedule" rule. Instead:
- When customer says "cancel" → ask: "Would you like to cancel your appointment, or reschedule to a different date?"
- If cancel → call `cancel_appointment` (pure cancel + reason note)
- If reschedule → call `reschedule_appointment` (cancel + new dates)

### 3. Confirmation + Reason

Cancel is a write action — require explicit confirmation before calling the API:
- "Are you sure you want to cancel your [project type] appointment on [date]?"
- Only call the API after customer confirms
- After cancellation, ask: "Can I note down the reason for the cancellation?"
- Post the reason as a project note via `add_note`

### 4. Cancellation Note Format

Since the cancel API is GET-only with no body, the cancellation reason is captured separately via add-note:

```
CANCELLATION REASON: [customer's stated reason]
Cancelled via AI Scheduling Assistant on [date] at [time] UTC.
Previously scheduled: [date] [time slot].
```

## Open Questions

1. Should the bot attempt to retain the customer before cancelling? (e.g., "Before I cancel, would you like to see other available dates?")
2. What happens after a successful cancel — does the project go to "Pending Reschedule" or "Not Scheduled"?
3. Are there any restrictions on which statuses can be cancelled (beyond what `ProjectStatusRules.can_cancel` already enforces)?
4. Is there a list of standard cancellation reasons to offer (e.g., "Schedule conflict", "No longer needed", "Going with another provider", "Other")?
