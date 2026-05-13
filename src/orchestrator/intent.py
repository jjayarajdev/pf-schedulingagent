"""Intent classifier — explicit domain-specific intent detection for scheduling.

Runs Sonnet on Bedrock with a structured-output prompt. Returns a typed result
the scheduling agent and tool guards can use to make safer decisions
(particularly around irreversible writes like ``confirm_appointment``).

Designed to sit upstream of the AgentSquad orchestrator so the agent's prompt
receives both raw text AND structured intent labels.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

import boto3
from botocore.config import Config

from config import get_settings

logger = logging.getLogger(__name__)


IntentLabel = Literal[
    "explicit_booking_confirmation",
    "ambiguous_affirm",
    "explicit_decline",
    "frustrated_decline",
    "new_constraint",
    "reschedule_request",
    "cancel_request",
    "info_query",
    "identification",
    "transfer_request",
    "schedule_request",
    "unclear",
]


@dataclass
class IntentResult:
    intent: IntentLabel
    confidence: float
    constraints: list[str] = field(default_factory=list)
    explicit_words: list[str] = field(default_factory=list)
    conflicts_with_prior: bool = False
    reasoning: str = ""
    elapsed_ms: int = 0
    raw_text: str = ""

    def authorizes_booking(self) -> bool:
        """Returns True only on unambiguous booking confirmation."""
        return (
            self.intent == "explicit_booking_confirmation"
            and self.confidence >= 0.8
            and not self.conflicts_with_prior
        )

    def authorizes_write(self) -> bool:
        """For any state-changing tool (confirm, cancel, reschedule submit)."""
        return self.intent in (
            "explicit_booking_confirmation",
            "cancel_request",
            "reschedule_request",
        ) and self.confidence >= 0.7

    def to_context_snippet(self) -> str:
        """Compact string to inject into the scheduling agent's prompt."""
        lines = [
            f"detected_intent: {self.intent} (confidence: {self.confidence:.2f})",
        ]
        if self.constraints:
            lines.append(f"customer_constraints: {self.constraints}")
        if self.explicit_words:
            lines.append(f"explicit_yes_no_words: {self.explicit_words}")
        if self.conflicts_with_prior:
            lines.append("WARNING: this response conflicts with a constraint the customer stated earlier")
        if self.reasoning:
            lines.append(f"classifier_reasoning: {self.reasoning}")
        return "\n".join(lines)


_SYSTEM_PROMPT = """\
You are an intent classifier for a home-improvement scheduling phone bot.

Your job: read the conversation history and the customer's latest utterance,
then return a structured JSON object describing what the customer means.

Be CONSERVATIVE about confirmations. The bot is about to take real actions
(cancel appointments, book appointments) — wrongly classifying ambiguous
sounds as confirmation causes real customer harm.

INTENT LABELS:

- explicit_booking_confirmation
    Customer said unambiguous booking words: "book it", "yes, please book that",
    "go ahead and schedule it", "confirm that appointment", "yes confirm".
    NOT this label: "yeah", "yep", "uh-huh", "ok", "sure" alone.

- ambiguous_affirm
    A short affirmative sound that COULD mean confirmation or COULD mean
    "I'm following you, keep going". Includes: "yeah", "yep", "ok", "sure",
    "right", "uh-huh", "mhm". Even after a question, treat as ambiguous
    unless the customer also re-stated the action.

- explicit_decline
    "No", "I don't want that", "cancel that", "stop", "don't book it".

- frustrated_decline
    Decline with frustration markers: "No. No.", "No, I just said —",
    "that's not what I wanted", "this is wrong", repeated "no" twice or more,
    or "no" combined with curse words / "joke" / "ridiculous".

- new_constraint
    The customer is telling you a hard constraint that future slot options
    must satisfy. "Only mornings", "before 12 PM", "weekdays only",
    "Thursdays at 10 AM only", "after 5", "not this weekend".
    Extract the constraint text into `constraints`.

- cancel_request
    "Cancel my appointment today", "I need to cancel".

- reschedule_request
    "Reschedule", "move my appointment", "different day", "I need a new date".

- schedule_request
    Initial scheduling intent: "Schedule appointment", "book a measurement".

- info_query
    Wants information, no action: "what time is my appointment",
    "when is the installer coming", "what's my project status".

- identification
    Caller is supplying identifying info the bot asked for. This is the
    common store-flow authentication pattern: "My PO number is 556677",
    "Project number 240367783", "Yeah, I have a PO number, it's 556677",
    "It's project two four zero three six seven seven eight three", or
    simply reciting digits. The caller is NOT asking a question or
    requesting an action — they are answering a lookup prompt. Prefer
    this label over `unclear` whenever the utterance contains a
    project number, PO number, or a string of digits offered as ID.

- transfer_request
    "Speak to a person", "talk to a human", "agent", "representative",
    "operator".

- unclear
    Off-topic, unintelligible, or you genuinely cannot tell.

RULES:
1. If the customer states a time constraint AND a scheduling intent in the
   same utterance, prefer the action intent but ALWAYS extract the constraint.
2. If the bot's most recent utterance offered a slot that VIOLATES a
   constraint the customer mentioned earlier in the conversation, set
   `conflicts_with_prior: true`.
3. Confidence is your probability that the label is correct.
   - 0.9+ : utterance is textbook for this label
   - 0.7-0.9 : reasonable confidence, context-dependent
   - 0.5-0.7 : leaning this way but ambiguous
   - <0.5 : prefer `unclear` or `ambiguous_affirm` instead
4. `explicit_words` should list the lowercased yes/no/confirm/decline words
   actually present in the utterance.
5. NEVER classify ambiguous "yeah" as explicit_booking_confirmation, even if
   the bot just asked "should I book this?" — that's a read-back trap.
6. When the utterance contains a clear identifier (PO number, project number,
   string of digits given as ID, or "I have a [PO/project] number, it's X"),
   classify as `identification`. Do NOT use `unclear` for this case.

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no commentary:

{
  "intent": "<label>",
  "confidence": <float 0-1>,
  "constraints": ["<extracted constraint text>"],
  "explicit_words": ["<yes/no word actually used>"],
  "conflicts_with_prior": <true|false>,
  "reasoning": "<one sentence>"
}
"""


def _format_history(history: list[dict]) -> str:
    if not history:
        return "(no prior turns)"
    lines = []
    for turn in history[-8:]:
        role = turn.get("role", "?")
        text = turn.get("text", "").strip().replace("\n", " ")
        if not text:
            continue
        prefix = "Bot" if role in ("assistant", "ai", "bot") else "Customer"
        lines.append(f"  {prefix}: {text}")
    return "\n".join(lines) or "(no prior turns)"


def _build_user_message(history: list[dict], utterance: str) -> str:
    return (
        "Conversation so far (oldest first):\n"
        f"{_format_history(history)}\n\n"
        f'Customer just said: "{utterance.strip()}"\n\n'
        "Return the JSON object."
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Bedrock occasionally wraps JSON in markdown; pull the object out."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if m := _FENCE_RE.search(text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    first = text.find("{")
    last = text.rfind("}")
    if 0 <= first < last:
        try:
            return json.loads(text[first : last + 1])
        except json.JSONDecodeError:
            return None
    return None


_client = None


def _bedrock_client():
    global _client
    if _client is None:
        cfg = Config(read_timeout=10, connect_timeout=5, retries={"max_attempts": 2, "mode": "standard"})
        _client = boto3.client(
            "bedrock-runtime",
            region_name=get_settings().aws_region,
            config=cfg,
        )
    return _client


async def classify_intent(
    utterance: str,
    history: list[dict] | None = None,
    *,
    model_id: str | None = None,
) -> IntentResult:
    """Classify the customer's intent. Falls back to ``unclear`` on any error.

    `history` is a list of {role: 'user'|'assistant', text: str} dicts in
    chronological order. Only the last 8 turns are sent to the model.
    """
    if not utterance or not utterance.strip():
        return IntentResult(
            intent="unclear",
            confidence=0.0,
            raw_text=utterance,
            reasoning="empty utterance",
        )
    history = history or []
    model_id = model_id or get_settings().classifier_model_id
    user_msg = _build_user_message(history, utterance)
    start = time.monotonic()
    try:
        client = _bedrock_client()
        resp = client.converse(
            modelId=model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_msg}]}],
            inferenceConfig={
                "maxTokens": 400,
                "temperature": 0.0,
                "topP": 0.9,
            },
        )
        body = resp["output"]["message"]["content"][0]["text"]
        parsed = _extract_json(body) or {}
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = IntentResult(
            intent=parsed.get("intent", "unclear"),
            confidence=float(parsed.get("confidence", 0.0)),
            constraints=list(parsed.get("constraints", [])) or [],
            explicit_words=list(parsed.get("explicit_words", [])) or [],
            conflicts_with_prior=bool(parsed.get("conflicts_with_prior", False)),
            reasoning=parsed.get("reasoning", "") or "",
            elapsed_ms=elapsed_ms,
            raw_text=utterance,
        )
        logger.info(
            "Intent classified: %s conf=%.2f elapsed_ms=%d constraints=%s",
            result.intent,
            result.confidence,
            elapsed_ms,
            result.constraints,
        )
        return result
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Intent classifier failed (elapsed_ms=%d) — falling back to unclear: %s",
            elapsed_ms,
            exc,
        )
        return IntentResult(
            intent="unclear",
            confidence=0.0,
            elapsed_ms=elapsed_ms,
            raw_text=utterance,
            reasoning=f"classifier_error: {type(exc).__name__}",
        )


# ── Per-request intent context (read by tool handlers) ────────────────────


_intent_cvar: ContextVar[IntentResult | None] = ContextVar(
    "intent_result", default=None
)


class IntentContext:
    """Per-request intent classification result.

    The webhook handler calls ``IntentContext.set(result)`` after classifying
    the user's utterance; tool handlers read via ``IntentContext.get()`` to
    gate irreversible writes (e.g. ``confirm_appointment``).
    """

    @staticmethod
    def set(result: IntentResult | None) -> None:
        _intent_cvar.set(result)

    @staticmethod
    def get() -> IntentResult | None:
        return _intent_cvar.get()

    @staticmethod
    def clear() -> None:
        _intent_cvar.set(None)


__all__ = ["IntentContext", "IntentLabel", "IntentResult", "classify_intent"]
