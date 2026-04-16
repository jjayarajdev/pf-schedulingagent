# Vapi Custom LLM — Eliminate GPT-4o-mini from Phone Calls

## Steps

- [x] Step 1: Add call-ID auth cache to `src/channels/vapi.py`
- [x] Step 2: Create Custom LLM endpoint `src/channels/vapi_llm.py`
- [x] Step 3: Add `_build_custom_llm_assistant_config()` to `src/channels/vapi.py`
- [x] Step 4: Switch customer callers to Custom LLM config in `_handle_assistant_request()`
- [x] Step 5: Register router in `src/main.py`
- [x] Step 6: Create tests `tests/unit/test_vapi_llm.py`
- [x] Verify: All tests pass — 500 passed, 36 new, 1 pre-existing failure (unrelated)

## Rollback

Revert one line in `_handle_assistant_request()`:
```python
# Change back to:
return {"assistant": _build_assistant_config(greeting, webhook_secret, support_number, client_name, hours_context)}
```
