# Fix Store Caller Issues — Plan

## Issues (from call analysis)

| # | Priority | Issue | Root Cause |
|---|----------|-------|------------|
| 1 | P0 | GPT hallucinating project details (fabricates status, dates, technician names) when `ask_store_bot` returns vague/generic response | GPT prompt has no anti-hallucination rule; when tool returns "provide project ID", GPT invents plausible details |
| 2 | P0 | PO number never actually used for auth (6 identical tool calls, backend ignores PO) | GPT calls `ask_store_bot` with `question` but doesn't pass `lookup_type`/`lookup_value`; backend gets no PO |
| 3 | P1 | Session context lost between turns | GPT sends different `question` each time without session continuity; orchestrator may not maintain store session across tool calls |
| 4 | P1 | "Hold on" still used (forbidden filler) | Store prompt still has rotating filler rule with 4 phrases instead of single "One moment." |
| 5 | P2 | Project numbers read aloud | Rule exists but GPT ignores it; needs stronger CRITICAL enforcement |
| 6 | P2 | "Project Source" instead of "ProjectsForce" (TTS mispronounced) | Vapi TTS reads "ProjectsForce" as "Project Source" |
| 7 | P2 | Premature call end while user is mid-sentence | `endCallPhrases` too aggressive — "bye" matches mid-sentence; `silenceTimeoutSeconds` may be too short |
| 8 | P3 | Garbled speech "From ProjectsForce will reach out" | `customer_instruction` when no transfer available has awkward phrasing |

---

## Fix Plan

### Fix 1: Anti-hallucination rule (P0) — `_build_store_assistant_config()`

Add a CRITICAL section to the store GPT prompt:

```
## CRITICAL: NEVER Fabricate Information
ONLY share information that ask_store_bot returned. If the tool says "provide project ID"
or gives a vague answer, tell the caller you need more info. NEVER invent project status,
dates, technician names, or any details. If you don't have data from the tool, say so.
```

**File**: `src/channels/vapi.py` — `_build_store_assistant_config()`, after `## AFTER RETAILER AUTHENTICATION`

### Fix 2: GPT not passing lookup_type/lookup_value (P0) — `_build_store_assistant_config()`

Strengthen the qualification step and tool call instructions:

- Step 3 in QUALIFICATION FLOW: make it explicit that `lookup_type` and `lookup_value` are REQUIRED on first call
- Add CRITICAL section: "On the FIRST ask_store_bot call, you MUST include lookup_type and lookup_value. Without these, auth will fail."
- Update tool description to be even more explicit

**File**: `src/channels/vapi.py` — `_build_store_assistant_config()` lines 1355-1357 and tool description line 1428-1431

### Fix 3: Session context between turns (P1) — `_handle_store_bot()`

The backend already caches creds in `_store_sessions` — that part works. The issue is:
- GPT doesn't pass `question` with context — the prompt says "pass user's EXACT words" which may lack context
- After auth, subsequent calls work but the orchestrator session_id uses `vapi-{call_id}`, which DOES maintain context

The real fix: after authentication succeeds and `ask_store_bot` returns the auth success message, GPT should immediately ask the user's question in the same turn or next turn. Add prompt guidance:

```
- After successful authentication, the tool returns project info.
  Present that info to the caller, then ask if they need anything else.
- For follow-up questions, the system remembers the project context.
  Just pass the caller's words — no need to re-authenticate.
```

**File**: `src/channels/vapi.py` — `_build_store_assistant_config()`, `## AFTER RETAILER AUTHENTICATION` section

### Fix 4: Filler rule (P1) — `_build_store_assistant_config()`

Replace the rotating filler rule (lines 1388-1394) with the same single-filler rule used for inbound:

```
"9. FILLER RULES: Say 'One moment.' ONLY when the user asks a NEW question
that requires a tool call. Do NOT say any filler when the user is just replying
to your question. NEVER say 'Hold on', 'Wait', 'Hang on', 'Just a sec',
'Give me a moment', 'Let me check', 'Let me pull that up', or 'One second'.
The ONLY allowed filler is 'One moment.' — nothing else."
```

**File**: `src/channels/vapi.py` — lines 1388-1394

### Fix 5: Project numbers read aloud (P2) — `_build_store_assistant_config()`

The rule exists at line 1384-1385 but GPT ignores it. Elevate to CRITICAL with examples:

```
## CRITICAL: Never Read Project Numbers Aloud
NEVER read project numbers, IDs, or PO numbers aloud — they are long and unintelligible.
Instead, identify projects by type and status (e.g., "the flooring installation" or
"the window measurement"). If the caller has only one project, just say "your project".
```

**File**: `src/channels/vapi.py` — move from general rules to its own CRITICAL section

### Fix 6: "Project Source" TTS mispronunciation (P2) — `_build_store_assistant_config()`

Add TTS pronunciation guidance and spell out the company name phonetically:

```
- When saying the company name, say "Projects Force" (two words) — NOT "ProjectsForce" as one word.
```

Also update `client_name` references in the prompt to use spaced version for speech.

**File**: `src/channels/vapi.py` — `_build_store_assistant_config()`, name handling

### Fix 7: Premature call end (P2) — `_build_store_assistant_config()`

- Remove "bye" from `endCallPhrases` — too short, matches mid-sentence
- Keep "bye bye", "bye now", "goodbye" which are intentional
- Increase `silenceTimeoutSeconds` from 45 → 60 for store callers (retailers may be multitasking)

**File**: `src/channels/vapi.py` — lines 1477-1483

### Fix 8: Garbled "From ProjectsForce will reach out" (P3) — `_build_store_assistant_config()`

Fix the `customer_instruction` when no transfer available (lines 1329-1333):

Before:
```python
f"say 'I don't have your account on file right now. "
f"Someone from {name} will reach out to you regarding this. "
```

After:
```python
f"say 'I don't have your account on file right now. "
f"Our team at {name} will reach out to you shortly. "
```

Also apply Fix 6's TTS-friendly name.

**File**: `src/channels/vapi.py` — lines 1329-1333

---

## Files Changed

| File | Action | Changes |
|------|--------|---------|
| `src/channels/vapi.py` | MODIFY | All 8 fixes in `_build_store_assistant_config()` + endCallPhrases |
| `tests/unit/test_vapi.py` | MODIFY | Update/add tests for store config changes |

## Implementation Order

1. Fix 1 + Fix 2 (P0s — anti-hallucination + PO auth) — biggest impact
2. Fix 4 (P1 — filler) — quick, matches inbound fix
3. Fix 3 (P1 — session context) — prompt improvement
4. Fix 5 + Fix 6 (P2 — project numbers + TTS) — prompt improvements
5. Fix 7 (P2 — premature end) — config change
6. Fix 8 (P3 — garbled speech) — wording fix

## Verification

- `uv run pytest tests/unit/test_vapi.py` — all tests pass
- Deploy to QA → test with store caller scenario
