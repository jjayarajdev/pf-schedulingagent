"""Tests for Vapi Custom LLM endpoint — POST /vapi/chat/completions."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _mock_vapi_secrets():
    """Provide a test Vapi secret for both webhook and Custom LLM auth."""
    mock_secrets = MagicMock()
    mock_secrets.vapi_api_key = "test-vapi-secret-123"
    with (
        patch("channels.vapi.get_secrets", return_value=mock_secrets),
        patch("channels.vapi_llm.get_secrets", return_value=mock_secrets),
    ):
        yield


def _vapi_headers():
    """Standard Vapi webhook headers with valid secret."""
    return {"x-vapi-secret": "test-vapi-secret-123"}


def _auth_headers():
    """Authorization header for Custom LLM auth (Vapi sends Bearer token)."""
    return {"Authorization": "Bearer test-vapi-secret-123"}


def _chat_completions_body(
    user_message: str = "What are my projects?",
    call_id: str = "test-call-123",
    phone: str = "+15551234567",
) -> dict:
    """Build a minimal Vapi Custom LLM request body."""
    return {
        "model": "scheduling-agent",
        "messages": [
            {"role": "system", "content": "You are J, a friendly phone assistant."},
            {"role": "user", "content": user_message},
        ],
        "stream": True,
        "call": {
            "id": call_id,
            "customer": {"number": phone},
        },
    }


def _parse_sse_chunks(response_text: str) -> list[dict]:
    """Parse SSE response text into a list of chunk dicts."""
    chunks = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            data = line[len("data: "):]
            chunks.append(json.loads(data))
    return chunks


def _mock_orchestrator_response(text: str, agent_name: str = "scheduling_agent"):
    """Create a mock AgentSquad response."""
    mock_output = MagicMock()
    mock_output.content = [{"text": text}]

    mock_metadata = MagicMock()
    mock_metadata.agent_name = agent_name

    mock_response = MagicMock()
    mock_response.output = mock_output
    mock_response.metadata = mock_metadata
    return mock_response


# ── Auth Tests ────────────────────────────────────────────────────────────


class TestAuth:
    """Authentication for Custom LLM endpoint."""

    def test_missing_secret_rejected(self, client):
        resp = client.post(
            "/vapi/chat/completions",
            json=_chat_completions_body(),
        )
        assert resp.status_code == 401

    def test_wrong_secret_rejected(self, client):
        resp = client.post(
            "/vapi/chat/completions",
            json=_chat_completions_body(),
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    def test_valid_secret_accepted(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("Here are your projects.")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t", "client_id": "c"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )
        assert resp.status_code == 200


# ── Message Extraction Tests ─────────────────────────────────────────────


class TestMessageExtraction:
    """Parsing the messages array from Vapi."""

    def test_extracts_latest_user_message(self):
        from channels.vapi_llm import _extract_last_user_message

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        assert _extract_last_user_message(messages) == "Second question"

    def test_handles_multi_part_content(self):
        from channels.vapi_llm import _extract_last_user_message

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "world"},
                ],
            }
        ]
        assert _extract_last_user_message(messages) == "Hello world"

    def test_returns_empty_for_no_user_message(self):
        from channels.vapi_llm import _extract_last_user_message

        messages = [{"role": "system", "content": "System prompt"}]
        assert _extract_last_user_message(messages) == ""

    def test_empty_message_returns_fallback_response(self, client):
        body = _chat_completions_body()
        body["messages"] = [{"role": "system", "content": "System prompt"}]

        with patch("channels.vapi_llm.get_call_auth", return_value=None):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        assert "I didn't catch that" in resp.text


# ── SSE Format Tests ─────────────────────────────────────────────────────


class TestSSEFormat:
    """Verify response matches OpenAI chat.completion.chunk schema."""

    def test_sse_format_correct(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("Your project is ready to schedule.")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        chunks = _parse_sse_chunks(resp.text)
        assert len(chunks) >= 2  # at least filler + content + done

        # Every chunk has the required fields
        for chunk in chunks:
            assert "id" in chunk
            assert chunk["object"] == "chat.completion.chunk"
            assert "choices" in chunk
            assert len(chunk["choices"]) == 1
            assert "delta" in chunk["choices"][0]

    def test_filler_emitted_first(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("Your project is ready.")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        chunks = _parse_sse_chunks(resp.text)
        first_chunk = chunks[0]
        first_content = first_chunk["choices"][0]["delta"].get("content", "")
        assert "<flush />" in first_content
        # First chunk also has role
        assert first_chunk["choices"][0]["delta"].get("role") == "assistant"

    def test_done_sentinel(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("Done.")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        assert "data: [DONE]" in resp.text

    def test_finish_reason_stop(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("Done.")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        chunks = _parse_sse_chunks(resp.text)
        last_chunk = chunks[-1]
        assert last_chunk["choices"][0]["finish_reason"] == "stop"


# ── Orchestrator Integration Tests ───────────────────────────────────────


class TestOrchestratorIntegration:
    """Verify orchestrator is called correctly."""

    def test_orchestrator_called_with_correct_params(self, client):
        body = _chat_completions_body(user_message="Show my projects")
        mock_response = _mock_orchestrator_response("Here are your projects.")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        creds = {
            "bearer_token": "test-token",
            "client_id": "c1",
            "user_id": "u1",
        }
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value=creds),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        mock_orch.route_request.assert_called_once()
        call_kwargs = mock_orch.route_request.call_args
        assert call_kwargs.kwargs["user_input"] == "Show my projects"
        assert call_kwargs.kwargs["session_id"] == "vapi-test-call-123"
        assert call_kwargs.kwargs["additional_params"] == {"channel": "vapi"}

    def test_voice_formatting_strips_markdown(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("**Bold** and *italic* text")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        # Extract all content from chunks (skip filler and done)
        chunks = _parse_sse_chunks(resp.text)
        content = ""
        for chunk in chunks:
            delta = chunk["choices"][0]["delta"]
            content += delta.get("content", "")

        # Markdown should be stripped
        assert "**" not in content
        assert "*italic*" not in content
        assert "Bold" in content
        assert "italic" in content


# ── Auth Context Tests ───────────────────────────────────────────────────


class TestAuthContext:
    """Verify AuthContext is populated from cached creds."""

    def test_auth_context_from_cache(self, client):
        body = _chat_completions_body()
        creds = {
            "bearer_token": "cached-jwt",
            "client_id": "client-42",
            "customer_id": "cust-99",
            "user_id": "user-7",
            "user_name": "Jane Doe",
            "timezone": "US/Central",
            "support_number": "+15559876543",
        }

        mock_response = _mock_orchestrator_response("Hello!")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)

        auth_set_calls = []
        original_set = None

        def capture_auth_set(**kwargs):
            auth_set_calls.append(kwargs)
            original_set(**kwargs)

        from auth.context import AuthContext

        original_set = AuthContext.set

        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value=creds),
            patch("channels.vapi_llm.AuthContext") as mock_auth,
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            mock_auth.set = MagicMock()
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        # AuthContext.set was called with the cached creds
        mock_auth.set.assert_called_once()
        call_kwargs = mock_auth.set.call_args.kwargs
        assert call_kwargs["auth_token"] == "cached-jwt"
        assert call_kwargs["client_id"] == "client-42"
        assert call_kwargs["customer_id"] == "cust-99"

    def test_no_cache_logs_warning(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response("Hello!")
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)

        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value=None),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
            patch("channels.vapi_llm.logger") as mock_logger,
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        mock_logger.warning.assert_any_call("No cached auth for call_id=%s", "test-call-123")


# ── Transfer Detection Tests ─────────────────────────────────────────────


class TestTransferDetection:
    """Transfer phrases emit tool_call chunk."""

    def test_transfer_detected(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response(
            "Let me transfer you to our support team."
        )
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        creds = {
            "bearer_token": "t",
            "support_number": "+15559876543",
        }
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value=creds),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        chunks = _parse_sse_chunks(resp.text)
        # Find the transfer chunk
        transfer_chunks = [
            c for c in chunks
            if "tool_calls" in c["choices"][0].get("delta", {})
        ]
        assert len(transfer_chunks) == 1
        tool_call = transfer_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_call["function"]["name"] == "transferCall"

        # finish_reason should be "tool_calls" (not "stop")
        done_chunks = [c for c in chunks if c["choices"][0].get("finish_reason") == "tool_calls"]
        assert len(done_chunks) == 1

    def test_no_transfer_without_support_number(self, client):
        body = _chat_completions_body()
        mock_response = _mock_orchestrator_response(
            "Let me transfer you to our support team."
        )
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        # No support_number in creds
        creds = {"bearer_token": "t"}
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value=creds),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        chunks = _parse_sse_chunks(resp.text)
        transfer_chunks = [
            c for c in chunks
            if "tool_calls" in c["choices"][0].get("delta", {})
        ]
        # Should NOT have a transfer chunk (no support number)
        assert len(transfer_chunks) == 0


# ── Guardrail Tests ──────────────────────────────────────────────────────


class TestGuardrails:
    """Hallucination detection and retry."""

    def test_guardrail_booking_retries(self, client):
        body = _chat_completions_body(user_message="yes, book it")

        # First response: hallucinated confirmation. Second: proper tool call.
        hallucinated = _mock_orchestrator_response("Your appointment is confirmed for Tuesday!")
        corrected = _mock_orchestrator_response("I've booked your appointment for Tuesday at 9 AM.")

        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(side_effect=[hallucinated, corrected])

        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
            patch("channels.vapi_llm.was_confirm_called", side_effect=[False, True]),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        # route_request should have been called twice (original + retry)
        assert mock_orch.route_request.call_count == 2
        # Second call should be the retry prompt
        retry_call = mock_orch.route_request.call_args_list[1]
        assert "confirm_appointment" in retry_call.kwargs["user_input"]

    def test_guardrail_time_slots_retries(self, client):
        body = _chat_completions_body(user_message="what times are available?")

        hallucinated = _mock_orchestrator_response(
            "Available times: 8:00 AM, 9:00 AM, 10:00 AM, 11:00 AM, 12:00 PM"
        )
        corrected = _mock_orchestrator_response("Let me check the actual available times for you.")

        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(side_effect=[hallucinated, corrected])

        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
            patch("channels.vapi_llm.was_time_slots_called", side_effect=[False, True]),
            patch("channels.vapi_llm.was_confirm_called", return_value=False),
            patch("channels.vapi_llm.was_cancel_called", return_value=False),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        assert mock_orch.route_request.call_count == 2


# ── Error Handling Tests ─────────────────────────────────────────────────


class TestErrorHandling:
    """Graceful error responses."""

    def test_orchestrator_error_returns_fallback(self, client):
        body = _chat_completions_body()
        mock_orch = MagicMock()
        mock_orch.route_request = AsyncMock(side_effect=Exception("Bedrock timeout"))
        with (
            patch("channels.vapi_llm.get_orchestrator", return_value=mock_orch),
            patch("channels.vapi_llm.get_call_auth", return_value={"bearer_token": "t"}),
            patch("channels.vapi_llm.log_conversation", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/vapi/chat/completions",
                json=body,
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # Should still return valid SSE with fallback message
        assert "data: [DONE]" in resp.text
        chunks = _parse_sse_chunks(resp.text)
        content = "".join(
            c["choices"][0]["delta"].get("content", "") for c in chunks
        )
        assert "trouble" in content.lower()


# ── Call Auth Cache Tests ────────────────────────────────────────────────


class TestCallAuthCache:
    """Call-ID auth cache in vapi.py."""

    def test_cache_set_and_get(self):
        from channels.vapi import _call_auth_cache, get_call_auth, remove_call_auth

        # Start clean
        _call_auth_cache.clear()

        # Set
        creds = {"bearer_token": "jwt-123", "client_id": "c1"}
        _call_auth_cache["call-abc"] = creds

        # Get
        assert get_call_auth("call-abc") == creds
        assert get_call_auth("nonexistent") is None

        # Remove
        remove_call_auth("call-abc")
        assert get_call_auth("call-abc") is None

        # Remove nonexistent — no error
        remove_call_auth("nonexistent")

        _call_auth_cache.clear()


# ── Custom LLM Config Tests ─────────────────────────────────────────────


class TestCustomLLMConfig:
    """_build_custom_llm_assistant_config() output."""

    def test_config_uses_custom_llm_provider(self):
        from channels.vapi import _build_custom_llm_assistant_config

        with patch("channels.vapi.get_settings") as mock_settings:
            mock_settings.return_value.environment = "dev"
            config = _build_custom_llm_assistant_config(
                first_message="Hello!",
                server_secret="secret-123",
                support_number="+15551234567",
                client_name="TestCo",
            )

        model = config["model"]
        assert model["provider"] == "custom-llm"
        assert model["model"] == "scheduling-agent"
        assert "/vapi/chat/completions" in model["url"]
        # Auth is via Vapi credential (Authorization header), not query param
        assert "?secret=" not in model["url"]

    def test_config_has_transfer_tool_only(self):
        from channels.vapi import _build_custom_llm_assistant_config

        with patch("channels.vapi.get_settings") as mock_settings:
            mock_settings.return_value.environment = "dev"
            config = _build_custom_llm_assistant_config(
                first_message="Hello!",
                support_number="+15551234567",
            )

        tools = config["model"]["tools"]
        # Only transferCall (no ask_scheduling_bot)
        assert len(tools) == 1
        assert tools[0]["type"] == "transferCall"

    def test_config_no_tools_without_support_number(self):
        from channels.vapi import _build_custom_llm_assistant_config

        with patch("channels.vapi.get_settings") as mock_settings:
            mock_settings.return_value.environment = "dev"
            config = _build_custom_llm_assistant_config(
                first_message="Hello!",
                support_number="",
            )

        tools = config["model"]["tools"]
        assert len(tools) == 0

    def test_config_includes_office_hours(self):
        from channels.vapi import _build_custom_llm_assistant_config

        hours_context = {
            "is_open": False,
            "prompt_snippet": "The office is currently CLOSED.",
        }
        with patch("channels.vapi.get_settings") as mock_settings:
            mock_settings.return_value.environment = "dev"
            config = _build_custom_llm_assistant_config(
                first_message="Hello!",
                hours_context=hours_context,
            )

        system_msg = config["model"]["messages"][0]["content"]
        assert "OFFICE HOURS" in system_msg
        assert "CLOSED" in system_msg

    def test_config_preserves_voice_and_transcriber(self):
        from channels.vapi import _build_custom_llm_assistant_config

        with patch("channels.vapi.get_settings") as mock_settings:
            mock_settings.return_value.environment = "dev"
            config = _build_custom_llm_assistant_config(
                first_message="Hello!",
            )

        assert config["voice"]["provider"] == "cartesia"
        assert config["transcriber"]["provider"] == "deepgram"
        assert config["firstMessage"] == "Hello!"
        assert "endCallPhrases" in config


# ── Helper Function Tests ────────────────────────────────────────────────


class TestHelpers:
    """Unit tests for helper functions."""

    def test_split_for_tts(self):
        from channels.vapi_llm import _split_for_tts

        text = "Hello there. How are you? I'm fine! Good."
        sentences = _split_for_tts(text)
        assert sentences == ["Hello there.", "How are you?", "I'm fine!", "Good."]

    def test_split_for_tts_single_sentence(self):
        from channels.vapi_llm import _split_for_tts

        text = "Just one sentence here."
        sentences = _split_for_tts(text)
        assert sentences == ["Just one sentence here."]

    def test_wants_transfer_positive(self):
        from channels.vapi_llm import _wants_transfer

        assert _wants_transfer("Let me transfer you to our support team.")
        assert _wants_transfer("I'll connect you with someone who can help.")
        assert _wants_transfer("I'm connecting you to our office.")

    def test_wants_transfer_negative(self):
        from channels.vapi_llm import _wants_transfer

        assert not _wants_transfer("Your appointment is confirmed.")
        assert not _wants_transfer("You have 3 projects ready to schedule.")

    def test_rotating_filler_varies(self):
        from channels.vapi_llm import _rotating_filler

        fillers = [_rotating_filler() for _ in range(5)]
        # Should not all be the same
        assert len(set(fillers)) > 1

    def test_openai_chunk_format(self):
        from channels.vapi_llm import _openai_chunk

        chunk = _openai_chunk("id-1", "Hello", role="assistant")
        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        parsed = json.loads(chunk[len("data: "):])
        assert parsed["id"] == "id-1"
        assert parsed["object"] == "chat.completion.chunk"
        assert parsed["choices"][0]["delta"]["content"] == "Hello"
        assert parsed["choices"][0]["delta"]["role"] == "assistant"
        assert parsed["choices"][0]["finish_reason"] is None

    def test_openai_done_chunk(self):
        from channels.vapi_llm import _openai_done_chunk

        chunk = _openai_done_chunk("id-1")
        parsed = json.loads(chunk[len("data: "):])
        assert parsed["choices"][0]["finish_reason"] == "stop"
        assert parsed["choices"][0]["delta"] == {}

    def test_openai_transfer_chunk(self):
        from channels.vapi_llm import _openai_transfer_chunk

        chunk = _openai_transfer_chunk("id-1", "+15551234567")
        parsed = json.loads(chunk[len("data: "):])
        tool_calls = parsed["choices"][0]["delta"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "transferCall"
        args = json.loads(tool_calls[0]["function"]["arguments"])
        assert args["destination"] == "+15551234567"

    def test_looks_like_booking_confirmation(self):
        from channels.vapi_llm import _looks_like_booking_confirmation

        assert _looks_like_booking_confirmation("Your appointment is confirmed for Tuesday!")
        assert _looks_like_booking_confirmation("You're all set!")
        assert not _looks_like_booking_confirmation("Would you like to confirm?")

    def test_looks_like_time_slot_list(self):
        from channels.vapi_llm import _looks_like_time_slot_list

        assert _looks_like_time_slot_list("Available: 8:00 AM, 9:00 AM, 10:00 AM")
        assert not _looks_like_time_slot_list("Your appointment is at 9:00 AM")
