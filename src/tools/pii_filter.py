"""PII scrubber — regex-based post-processing for store caller responses.

Store callers should not hear customer phone numbers, email addresses, or
street-level addresses.  This module provides a ``scrub_pii`` function that
strips those patterns from orchestrator response text.

Defense-in-depth layer: ``_extract_project_minimal()`` in scheduling.py also
excludes ``address1`` for store callers at the data level.
"""

import re

# Phone numbers: (xxx) xxx-xxxx, xxx-xxx-xxxx, +1xxxxxxxxxx, xxx.xxx.xxxx
_PHONE_RE = re.compile(
    r"(?<!\d)"  # not preceded by digit
    r"(?:"
    r"\+?1?[-.\s]?"  # optional country code
    r"\(?\d{3}\)?[-.\s]?"  # area code
    r"\d{3}[-.\s]?"  # exchange
    r"\d{4}"  # subscriber
    r")"
    r"(?!\d)"  # not followed by digit
)

# Email addresses
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Street addresses: number + street name + suffix
# e.g. "123 Main St", "4500 Oak Boulevard"
_STREET_SUFFIXES = (
    r"St(?:reet)?|Ave(?:nue)?|Blvd|Boulevard|Dr(?:ive)?|Ln|Lane|"
    r"Ct|Court|Rd|Road|Way|Pl(?:ace)?|Cir(?:cle)?|"
    r"Pkwy|Parkway|Ter(?:race)?|Trail|Trl|Loop|Run|Pass"
)
_STREET_RE = re.compile(
    rf"\d+\s+[\w\s]{{1,40}}\b(?:{_STREET_SUFFIXES})\b\.?",
    re.IGNORECASE,
)

_REDACTED = "[redacted]"


def scrub_pii(text: str) -> str:
    """Remove PII patterns from response text for store callers.

    Strips:
    - Phone numbers (US formats)
    - Email addresses
    - Street-level addresses (best-effort pattern match)
    """
    if not text:
        return text

    text = _PHONE_RE.sub(_REDACTED, text)
    text = _EMAIL_RE.sub(_REDACTED, text)
    text = _STREET_RE.sub(_REDACTED, text)

    # Clean up double-redacted artifacts
    text = re.sub(rf"(?:{re.escape(_REDACTED)}\s*)+", _REDACTED + " ", text)

    return text.strip()
