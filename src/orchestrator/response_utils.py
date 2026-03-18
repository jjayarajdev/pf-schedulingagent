"""Shared utilities for extracting text from AgentSquad responses."""


def extract_response_text(output) -> str:
    """Extract text from AgentResponse.output (string or ConversationMessage)."""
    if isinstance(output, str):
        return output

    if hasattr(output, "content") and output.content:
        text_parts = []
        for block in output.content:
            if isinstance(block, dict) and "text" in block:
                text_parts.append(block["text"])
            elif isinstance(block, str):
                text_parts.append(block)
        if text_parts:
            return "\n".join(text_parts)

    return str(output) if output else "I wasn't able to generate a response."
