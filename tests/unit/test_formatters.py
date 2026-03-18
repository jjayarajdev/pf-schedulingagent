"""Tests for SMS and voice response formatters."""

from channels.formatters import format_for_sms, format_for_voice


class TestFormatForSms:
    def test_empty_string(self):
        assert format_for_sms("") == ""

    def test_plain_text_unchanged(self):
        text = "Your appointment is on March 15 at 10:00 AM."
        result = format_for_sms(text)
        assert result == text

    def test_strips_bold_markdown(self):
        result = format_for_sms("**Important**: Your project is ready.")
        assert "**" not in result
        assert "Important" in result

    def test_strips_italic_markdown(self):
        result = format_for_sms("*Please confirm* your appointment.")
        assert result.startswith("Please confirm") or "Please confirm" in result
        # No stray asterisks
        assert result.count("*") == 0

    def test_strips_heading(self):
        result = format_for_sms("## Available Dates\nMonday\nTuesday")
        assert "##" not in result
        assert "Available Dates" in result

    def test_converts_link_to_text(self):
        result = format_for_sms("Visit [our site](https://example.com) for details.")
        assert "our site" in result
        assert "https://" not in result

    def test_removes_image_references(self):
        result = format_for_sms("See ![screenshot](path/to/image.png) for help.")
        assert "![" not in result

    def test_replaces_check_emoji(self):
        result = format_for_sms("\u2705 Appointment confirmed!")
        assert "[OK]" in result

    def test_replaces_warning_emoji(self):
        result = format_for_sms("\u26a0\ufe0f Warning: bad weather expected.")
        assert "[!]" in result

    def test_strips_remaining_emojis(self):
        result = format_for_sms("\U0001f389 Congrats! Your appointment is set.")
        # No emojis should remain
        assert all(ord(ch) < 128 for ch in result)

    def test_unicode_quotes_to_ascii(self):
        result = format_for_sms("\u201cHello\u201d he said")
        assert '"Hello"' in result

    def test_em_dash_to_double_dash(self):
        result = format_for_sms("Monday \u2014 the best day")
        assert "--" in result

    def test_truncation(self):
        long_text = "x" * 2000
        result = format_for_sms(long_text, max_length=100)
        assert len(result) <= 100

    def test_truncation_prefers_sentence_boundary(self):
        text = "First sentence. " * 100  # Long text with sentence boundaries
        result = format_for_sms(text, max_length=200)
        assert len(result) <= 200
        assert result.endswith(".")

    def test_project_number_preserved(self):
        text = "Project 21083_09PF05VD_1762166550719 is scheduled."
        result = format_for_sms(text)
        assert "21083_09PF05VD_1762166550719" in result

    def test_ascii_safe_output(self):
        result = format_for_sms("Hello \U0001f31f World \u2603 Test")
        assert all(ord(ch) < 128 or ch in " \n\t" for ch in result)

    def test_compact_whitespace(self):
        result = format_for_sms("Hello    world\n\n\n\nGoodbye")
        assert "    " not in result
        assert "\n\n\n" not in result


class TestFormatForVoice:
    def test_empty_string(self):
        assert format_for_voice("") == ""

    def test_strips_bold(self):
        result = format_for_voice("**Important**: check your schedule.")
        assert "**" not in result
        assert "Important" in result

    def test_strips_heading(self):
        result = format_for_voice("## Results\nYour appointment is set.")
        assert "##" not in result
        assert "Results" in result

    def test_strips_code_blocks(self):
        result = format_for_voice("Here:\n```python\nprint('hello')\n```\nDone.")
        assert "```" not in result
        assert "print" not in result

    def test_strips_images(self):
        result = format_for_voice("See ![alt](url) for details.")
        assert "![" not in result

    def test_strips_links_keeps_text(self):
        result = format_for_voice("Click [here](http://example.com).")
        assert "here" in result
        assert "http://" not in result

    def test_strips_bullet_points(self):
        result = format_for_voice("Options:\n- Monday\n- Tuesday")
        assert "- " not in result or result.count("-") == 0
        assert "Monday" in result

    def test_strips_inline_code(self):
        result = format_for_voice("Use the `submit` button.")
        assert "`" not in result
        assert "submit" in result
