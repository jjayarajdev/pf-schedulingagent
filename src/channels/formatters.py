"""Channel response formatters — SMS-safe text and voice-optimized output.

Ported from v1.2.9 lambda/orchestrator/sms_formatter.py with additions for
voice formatting. The SMS pipeline ensures plain ASCII text that passes through
AWS End User Messaging (Pinpoint) without rejection.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── Ordinal number to words (for TTS clarity) ─────────────────────────────
_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
    6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
    11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth",
    15: "fifteenth", 16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
    19: "nineteenth", 20: "twentieth", 21: "twenty-first", 22: "twenty-second",
    23: "twenty-third", 24: "twenty-fourth", 25: "twenty-fifth",
    26: "twenty-sixth", 27: "twenty-seventh", 28: "twenty-eighth",
    29: "twenty-ninth", 30: "thirtieth", 31: "thirty-first",
}

_ORDINAL_SUFFIX_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b")


def _ordinal_to_words(match: re.Match) -> str:
    """Convert '21st' → 'twenty-first', '3rd' → 'third', etc."""
    num = int(match.group(1))
    return _ORDINALS.get(num, match.group(0))


# Years that TTS mangles — spoken form for voice channel
_YEAR_WORDS = {
    "2025": "twenty twenty-five",
    "2026": "twenty twenty-six",
    "2027": "twenty twenty-seven",
    "2028": "twenty twenty-eight",
}

# ============================================================================
# EMOJI TO TEXT MAPPINGS
# Replace common emojis with text equivalents to preserve meaning
# ============================================================================
EMOJI_TEXT_MAP: dict[str, str] = {
    # Weather emojis
    "\u2600\ufe0f": "(sunny)",  # sun with rays
    "\u2600": "(sunny)",  # sun
    "\u26c5": "(partly cloudy)",  # sun behind cloud
    "\U0001f324": "(mostly sunny)",  # sun behind small cloud
    "\U0001f325": "(partly cloudy)",  # sun behind large cloud
    "\u2601\ufe0f": "(cloudy)",  # cloud (with variation selector)
    "\u2601": "(cloudy)",  # cloud
    "\U0001f326": "(rain)",  # sun behind rain cloud
    "\U0001f327": "(rainy)",  # cloud with rain
    "\u26c8": "(stormy)",  # cloud with lightning and rain
    "\U0001f329": "(lightning)",  # cloud with lightning
    "\U0001f328": "(snow)",  # cloud with snow
    "\u2744\ufe0f": "(snow)",  # snowflake (with variation selector)
    "\u2744": "(snow)",  # snowflake
    "\U0001f32b": "(foggy)",  # fog
    "\U0001f32c": "(windy)",  # wind face
    "\U0001f321": "",  # thermometer — remove
    "\U0001f4a7": "(rain)",  # droplet
    # Status emojis
    "\u26a0\ufe0f": "[!]",  # warning (with variation selector)
    "\u26a0": "[!]",  # warning
    "\u2705": "[OK]",  # check mark
    "\u2714\ufe0f": "[OK]",  # heavy check mark (with variation selector)
    "\u2714": "[OK]",  # heavy check mark
    "\u274c": "[X]",  # cross mark
    "\u274e": "[X]",  # cross mark (negative squared)
    "\u2716\ufe0f": "[X]",  # heavy multiplication x (with variation selector)
    "\u2716": "[X]",  # heavy multiplication x
    "\u2757": "[!]",  # exclamation mark
    "\u2755": "[!]",  # white exclamation mark
    "\u2753": "[?]",  # question mark
    "\u2754": "[?]",  # white question mark
    "\U0001f6a8": "[ALERT]",  # rotating light
    "\U0001f4a1": "",  # light bulb — remove
    "\U0001f50d": "",  # magnifying glass — remove
    # Common UI / scheduling emojis
    "\U0001f4c5": "",  # calendar — remove
    "\U0001f4c6": "",  # tear-off calendar — remove
    "\U0001f4cb": "[list]",  # clipboard
    "\U0001f4c4": "",  # page facing up — remove
    "\U0001f4dd": "",  # memo — remove
    "\U0001f551": "",  # clock face two o'clock — remove
    "\U0001f552": "",  # clock face three o'clock — remove
    "\U0001f553": "",  # clock face four o'clock — remove
    "\U0001f554": "",  # clock face five o'clock — remove
    "\U0001f555": "",  # clock face six o'clock — remove
    "\U0001f556": "",  # clock face seven o'clock — remove
    "\U0001f557": "",  # clock face eight o'clock — remove
    "\U0001f558": "",  # clock face nine o'clock — remove
    "\U0001f559": "",  # clock face ten o'clock — remove
    "\U0001f55a": "",  # clock face eleven o'clock — remove
    "\U0001f55b": "",  # clock face twelve o'clock — remove
    "\U0001f550": "",  # clock face one o'clock — remove
    "\U0001f4cd": "",  # round pushpin — location pin — remove
    "\U0001f3e0": "",  # house — remove
    "\U0001f527": "",  # wrench — remove
    "\U0001f6e0": "",  # hammer and wrench — remove
    "\U0001f4de": "",  # telephone receiver — remove
    "\U0001f4e7": "",  # e-mail — remove
    "\U0001f44d": "(OK)",  # thumbs up
    "\U0001f44e": "(NO)",  # thumbs down
    "\U0001f389": "",  # party popper — remove
    "\U0001f31f": "",  # glowing star — remove
    "\u2b50": "",  # star — remove
    "\U0001f3d7": "",  # building construction — remove
    "\U0001f477": "",  # construction worker — remove
    "\U0001f6a7": "",  # construction sign — remove
    "\U0001f4f1": "",  # mobile phone — remove
    "\U0001f4bb": "",  # laptop — remove
    "\U0001f464": "",  # bust in silhouette — remove
    "\U0001f465": "",  # busts in silhouette — remove
    "\U0001f4ca": "",  # bar chart — remove
    "\U0001f4c8": "",  # chart increasing — remove
    "\U0001f4c9": "",  # chart decreasing — remove
}

# ============================================================================
# UNICODE TO ASCII MAPPINGS
# Replace special unicode characters with ASCII equivalents
# ============================================================================
UNICODE_ASCII_MAP: dict[str, str] = {
    # Quotes
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u201a": ",",  # single low-9 quotation mark
    "\u201e": '"',  # double low-9 quotation mark
    "\u2032": "'",  # prime
    "\u2033": '"',  # double prime
    "\u00ab": '"',  # left-pointing double angle quotation mark
    "\u00bb": '"',  # right-pointing double angle quotation mark
    # Dashes
    "\u2013": "-",  # en dash
    "\u2014": "--",  # em dash
    "\u2015": "--",  # horizontal bar
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    # Spaces
    "\u00a0": " ",  # non-breaking space
    "\u2002": " ",  # en space
    "\u2003": " ",  # em space
    "\u2009": " ",  # thin space
    "\u200a": " ",  # hair space
    "\u200b": "",  # zero-width space
    "\u200c": "",  # zero-width non-joiner
    "\u200d": "",  # zero-width joiner
    "\ufeff": "",  # byte order mark
    # Other punctuation
    "\u2026": "...",  # horizontal ellipsis
    "\u2022": "*",  # bullet
    "\u2023": ">",  # triangular bullet
    "\u2043": "-",  # hyphen bullet
    "\u00b7": "*",  # middle dot
    "\u00b0": " degrees",  # degree sign
    "\u2103": "C",  # degree celsius
    "\u2109": "F",  # degree fahrenheit
    # Arrows
    "\u2192": "->",  # rightwards arrow
    "\u2190": "<-",  # leftwards arrow
    "\u2191": "^",  # upwards arrow
    "\u2193": "v",  # downwards arrow
    "\u21d2": "=>",  # rightwards double arrow
    "\u21d0": "<=",  # leftwards double arrow
}

# ── Compiled regex patterns (module-level for performance) ────────────────

# Project number pattern: 2+ segments of alphanumeric separated by underscores,
# must contain both letters and digits (e.g. 21083_09PF05VD_1762166550719)
_PROJECT_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,})(?![A-Za-z0-9_])"
)
_PROJECT_NUM_PLACEHOLDER = "[[PROJNUM{}]]"

# Emoji unicode ranges — catches anything the EMOJI_TEXT_MAP missed
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f700-\U0001f77f"  # alchemical symbols
    "\U0001f780-\U0001f7ff"  # geometric shapes extended
    "\U0001f800-\U0001f8ff"  # supplemental arrows-c
    "\U0001f900-\U0001f9ff"  # supplemental symbols and pictographs
    "\U0001fa00-\U0001fa6f"  # chess symbols
    "\U0001fa70-\U0001faff"  # symbols and pictographs extended-a
    "\U00002702-\U000027b0"  # dingbats
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U00002600-\U000026ff"  # misc symbols
    "\U00002300-\U000023ff"  # misc technical
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0001f000-\U0001f02f"  # mahjong tiles
    "\U0001f0a0-\U0001f0ff"  # playing cards
    "]+",
    flags=re.UNICODE,
)

# Markdown patterns
_RE_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_BOLD_ASTERISK = re.compile(r"\*\*(.+?)\*\*")
_RE_ITALIC_ASTERISK = re.compile(r"\*(.+?)\*")
_RE_BOLD_UNDERSCORE = re.compile(r"__(.+?)__")
_RE_ITALIC_UNDERSCORE = re.compile(r"_(.+?)_")
_RE_INLINE_CODE = re.compile(r"`(.+?)`")
_RE_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_RE_NUMBERED = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)


# ── Public API ────────────────────────────────────────────────────────────


def format_for_sms(text: str, max_length: int = 1500) -> str:
    """Format response for SMS delivery — plain ASCII, no emojis.

    Eight-step pipeline:
    1. Protect project numbers (preserve underscores from markdown stripping)
    2. Replace known emojis with text equivalents
    3. Strip remaining emojis via unicode ranges
    4. Remove markdown formatting
    5. Restore project numbers
    6. Normalize unicode (fancy quotes -> ASCII, em-dash -> --, etc.)
    7. Compact whitespace
    8. Truncate to max_length chars

    Args:
        text: Raw response from orchestrator.
        max_length: Maximum message length (default 1500; SMS hard limit is 1600).

    Returns:
        SMS-safe plain text response.
    """
    if not text:
        return text

    try:
        # Step 1: Protect project numbers before any processing
        text, placeholders = _protect_project_numbers(text)

        # Step 2: Replace known emojis with text equivalents
        for emoji, replacement in EMOJI_TEXT_MAP.items():
            text = text.replace(emoji, replacement)

        # Step 3: Strip remaining emojis
        text = _EMOJI_RE.sub("", text)

        # Step 4: Remove markdown formatting
        text = _remove_markdown(text)

        # Step 5: Restore protected project numbers
        text = _restore_project_numbers(text, placeholders)

        # Step 6: Replace special unicode with ASCII
        for unicode_char, ascii_char in UNICODE_ASCII_MAP.items():
            text = text.replace(unicode_char, ascii_char)

        # Step 7: Compact whitespace
        text = _compact_whitespace(text)

        # Step 8: Truncate if too long
        text = _truncate(text, max_length)

        # Final safety: strip anything non-ASCII that slipped through
        text = _ensure_ascii_safe(text)

        return text.strip()

    except Exception:
        logger.exception("Error formatting for SMS")
        return _ensure_ascii_safe(text)[:max_length]


def format_for_voice(text: str) -> str:
    """Format response for voice/TTS — strip markdown, keep concise.

    Removes formatting markers that a TTS engine would speak aloud
    (asterisks, hash signs, link syntax, etc.) while preserving the
    natural language content.
    """
    if not text:
        return text

    # Remove code blocks entirely (not useful for voice)
    text = _RE_CODE_BLOCK.sub("", text)
    # Remove image references
    text = _RE_IMAGE.sub("", text)
    # Convert links to just the link text
    text = _RE_LINK.sub(r"\1", text)
    # Remove bold/italic markers
    text = _RE_BOLD_ASTERISK.sub(r"\1", text)
    text = _RE_ITALIC_ASTERISK.sub(r"\1", text)
    text = _RE_BOLD_UNDERSCORE.sub(r"\1", text)
    text = _RE_ITALIC_UNDERSCORE.sub(r"\1", text)
    # Remove heading markers
    text = _RE_HEADING.sub("", text)
    # Remove bullet prefixes
    text = _RE_BULLET.sub("", text)
    # Remove numbered list prefixes
    text = _RE_NUMBERED.sub("", text)
    # Remove inline code markers
    text = _RE_INLINE_CODE.sub(r"\1", text)
    # Clean up excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ── TTS pronunciation fixes ──────────────────────────────────────
    # Convert ordinal dates to words: "April 21st" → "April twenty-first"
    text = _ORDINAL_SUFFIX_RE.sub(_ordinal_to_words, text)
    # Convert years to spoken form: "2026" → "twenty twenty-six"
    for year, words in _YEAR_WORDS.items():
        text = text.replace(year, words)

    return text.strip()


# ── Internal helpers ──────────────────────────────────────────────────────


def _protect_project_numbers(text: str) -> tuple[str, dict[str, str]]:
    """Detect and protect project numbers before markdown removal.

    Project numbers like ``21083_09PF05VD_1762166550719`` contain underscores
    that would be mangled by italic/bold stripping.  We replace them with
    safe placeholders and restore after markdown removal.

    Returns:
        A tuple of (modified text, placeholder->original mapping).
    """
    placeholders: dict[str, str] = {}

    def _replace(match: re.Match) -> str:
        value = match.group(1)
        has_letters = any(c.isalpha() for c in value)
        has_digits = any(c.isdigit() for c in value)
        if has_letters and has_digits:
            idx = len(placeholders)
            placeholder = _PROJECT_NUM_PLACEHOLDER.format(idx)
            placeholders[placeholder] = value
            return placeholder
        return value

    modified = _PROJECT_NUMBER_RE.sub(_replace, text)
    return modified, placeholders


def _restore_project_numbers(text: str, placeholders: dict[str, str]) -> str:
    """Restore protected project numbers after markdown removal."""
    for placeholder, original in placeholders.items():
        text = text.replace(placeholder, original)
    return text


def _remove_markdown(text: str) -> str:
    """Strip markdown formatting — bold, italic, headers, links, code, images."""
    text = _RE_CODE_BLOCK.sub("", text)
    text = _RE_IMAGE.sub("", text)
    text = _RE_LINK.sub(r"\1", text)
    text = _RE_BOLD_ASTERISK.sub(r"\1", text)
    text = _RE_ITALIC_ASTERISK.sub(r"\1", text)
    text = _RE_BOLD_UNDERSCORE.sub(r"\1", text)
    text = _RE_ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    text = re.sub(r"^\s*[-*]\s+", "- ", text, flags=re.MULTILINE)
    return text


def _compact_whitespace(text: str) -> str:
    """Reduce multiple spaces/newlines to reasonable limits."""
    # Multiple spaces/tabs to single space
    text = re.sub(r"[ \t]+", " ", text)
    # 3+ newlines to double newline
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Space before punctuation
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    # Double punctuation from emoji removal
    text = re.sub(r"([.,!?;:])\s*\1+", r"\1", text)
    # Trim each line
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines)


def _truncate(text: str, max_length: int) -> str:
    """Truncate to *max_length* chars, preferring sentence boundaries."""
    if len(text) <= max_length:
        return text

    truncated = text[: max_length - 3]

    # Try to break at sentence boundary
    last_period = truncated.rfind(". ")
    last_question = truncated.rfind("? ")
    last_exclaim = truncated.rfind("! ")
    best_break = max(last_period, last_question, last_exclaim)

    if best_break > max_length * 0.5:
        return truncated[: best_break + 1]

    # Otherwise break at word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_length * 0.8:
        return truncated[:last_space] + "..."

    return truncated + "..."


def _ensure_ascii_safe(text: str) -> str:
    """Keep only printable ASCII and common whitespace."""
    return "".join(ch for ch in text if ord(ch) < 128 or ch in " \n\t")
