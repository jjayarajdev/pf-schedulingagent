"""Shared utilities for extracting text from AgentSquad responses."""


def _extract_text_from_content(content) -> list[str]:
    """Recursively extract text from content blocks."""
    parts: list[str] = []
    if not content:
        return parts

    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
        elif hasattr(block, "content") and block.content:
            # Nested ConversationMessage — recurse
            parts.extend(_extract_text_from_content(block.content))
        elif hasattr(block, "text"):
            # Object with .text attribute
            parts.append(str(block.text))
    return parts


def extract_response_text(output) -> str:
    """Extract text from AgentResponse.output (string or ConversationMessage)."""
    if isinstance(output, str):
        return output

    if hasattr(output, "content") and output.content:
        parts = _extract_text_from_content(output.content)
        if parts:
            return "\n".join(parts)

    return str(output) if output else "I wasn't able to generate a response."
