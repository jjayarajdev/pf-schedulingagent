"""Tests for extract_response_text utility."""

from unittest.mock import MagicMock

from orchestrator.response_utils import extract_response_text


class TestExtractResponseText:
    def test_string_output(self):
        assert extract_response_text("Hello world") == "Hello world"

    def test_conversation_message_with_text_blocks(self):
        msg = MagicMock()
        msg.content = [{"text": "Part 1"}, {"text": "Part 2"}]
        result = extract_response_text(msg)
        assert "Part 1" in result
        assert "Part 2" in result

    def test_conversation_message_with_string_blocks(self):
        msg = MagicMock()
        msg.content = ["Just a string"]
        assert extract_response_text(msg) == "Just a string"

    def test_empty_output(self):
        result = extract_response_text("")
        assert result == ""

    def test_none_output(self):
        result = extract_response_text(None)
        assert "able" in result.lower() or result == ""

    def test_object_without_content(self):
        msg = MagicMock(spec=[])
        result = extract_response_text(msg)
        assert isinstance(result, str)
