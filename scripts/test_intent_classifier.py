"""Test harness for the intent classifier.

Runs a captured prod call through the classifier turn-by-turn and prints
the structured output side-by-side with the transcript.

Usage:
    PYTHONPATH=src uv run python scripts/test_intent_classifier.py
    PYTHONPATH=src uv run python scripts/test_intent_classifier.py --call vapi/12May/eloise.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

# The canonical Eloise (019e1c9d) regression case — captured from prod 2026-05-12.
ELOISE_TRANSCRIPT = """\
AI: Hello, Eloise. I'm Jay, your AI assistant from Window Treatments Unlimited. I can help you view your projects, check available dates, or schedule appointments. What would you like to do today?
User: I have to cancel today's appointment because my son has to go to work by 2 o'clock. He said he would only be home in the morning. We thought they understood that. And he has to be to work for 12. So we have to cancel today. Can you do that?
AI: This'll just take a sec.
AI: Your blinds measurement appointment for today at 1 PM has been canceled. The reason has been noted in your project file. If you'd like to reschedule for a time that works better with your son's schedule, I can help you find new dates and times. Would you like me to check availability for you?
User: He said Thursday, it would like, only at 10 o'clock in the morning if you had something like that.
AI: This will just take a sec.
AI: I have Thursday, May fourteenth available. But the only time slot is 12 PM. That might be cutting it close since your son needs to be at work by 12.
User: Yeah.
AI: Would you like me to check — Just a sec.
User: Yep.
User: To call you back I I can't be on the I'm at work. I'll
AI: Sorry. A few more seconds.
User: day.
AI: Just a sec.
User: No. You
AI: Sorry. A few more seconds.
AI: 1 moment.
AI: Your appointment request has been submitted for Thursday, May 14th at 12 PM.
User: No.
User: No.
"""


def parse_transcript(text: str) -> list[dict]:
    """Parse a 'AI: ... / User: ...' transcript into role/text turns."""
    turns: list[dict] = []
    for line in text.strip().splitlines():
        m = re.match(r"(AI|User|Bot|Customer):\s*(.+)$", line.strip())
        if not m:
            continue
        role = "assistant" if m.group(1) in ("AI", "Bot") else "user"
        text = m.group(2).strip()
        if text:
            turns.append({"role": role, "text": text})
    return turns


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


async def run(call_path: Path | None, model_id: str | None, predicted_path: Path | None) -> None:
    from orchestrator.intent import IntentResult, classify_intent

    predicted = None
    if predicted_path:
        data = json.loads(predicted_path.read_text())
        predicted = {p["turn"]: p["expected"] for p in data["predictions"]}
        print(f"[predicted mode] using golden outputs from {predicted_path}")
        print(f"[predicted mode] note: {data.get('_note', '')[:120]}\n")

    if call_path:
        turns = json.loads(call_path.read_text())
    else:
        turns = parse_transcript(ELOISE_TRANSCRIPT)

    print(f"\n{'='*120}")
    print(f"Test conversation: {call_path or 'Eloise (019e1c9d) — built-in'}")
    print(f"Total turns: {len(turns)}  ·  User turns: {sum(1 for t in turns if t['role'] == 'user')}")
    print(f"Model: {model_id or '<settings.classifier_model_id>'}")
    print("=" * 120)

    history: list[dict] = []
    user_index = 0
    results: list[dict] = []
    for t in turns:
        if t["role"] != "user":
            history.append(t)
            continue
        user_index += 1
        utterance = t["text"]
        # Show what the bot just said (for context)
        last_bot = next(
            (h["text"] for h in reversed(history) if h["role"] == "assistant"),
            "",
        )
        print()
        print(f"── Turn {user_index} " + "─" * 100)
        if last_bot:
            print(f"   Bot prior: {_truncate(last_bot, 110)}")
        print(f"   Customer : \033[1m{_truncate(utterance, 110)}\033[0m")

        if predicted is not None and user_index in predicted:
            p = predicted[user_index]
            result = IntentResult(
                intent=p["intent"],
                confidence=float(p["confidence"]),
                constraints=list(p.get("constraints", [])),
                explicit_words=list(p.get("explicit_words", [])),
                conflicts_with_prior=bool(p.get("conflicts_with_prior", False)),
                reasoning=p.get("reasoning", ""),
                elapsed_ms=0,
                raw_text=utterance,
            )
        else:
            result = await classify_intent(utterance, history, model_id=model_id)
        print(f"   → intent             : {result.intent}  (conf={result.confidence:.2f})")
        if result.explicit_words:
            print(f"     explicit_words     : {result.explicit_words}")
        if result.constraints:
            print(f"     constraints        : {result.constraints}")
        if result.conflicts_with_prior:
            print("     conflicts_with_prior: \033[91mTRUE\033[0m  ← would block irreversible writes")
        print(f"     reasoning          : {result.reasoning}")
        print(f"     elapsed_ms         : {result.elapsed_ms}")
        # What the guard would do
        if result.authorizes_booking():
            verdict = "\033[92mALLOWED\033[0m — confirm_appointment may proceed"
        elif result.intent in ("frustrated_decline", "explicit_decline"):
            verdict = "\033[91mHALT\033[0m — block any pending submission, offer alternatives"
        elif result.intent == "ambiguous_affirm":
            verdict = "\033[93mREAD-BACK REQUIRED\033[0m — bot must restate the action and ask for explicit yes"
        elif result.intent == "new_constraint":
            verdict = "\033[96mSTORE CONSTRAINT\033[0m — refilter future date options"
        else:
            verdict = "→ pass through to scheduling agent (no write authorization)"
        print(f"     guard verdict      : {verdict}")
        results.append(asdict(result))
        history.append(t)

    print()
    print("=" * 120)
    print("Summary")
    print("=" * 120)
    print(f"  Total user turns           : {user_index}")
    print(f"  Booking-authorized turns   : {sum(1 for r in results if r['intent'] == 'explicit_booking_confirmation')}")
    print(f"  Ambiguous-affirm turns     : {sum(1 for r in results if r['intent'] == 'ambiguous_affirm')}")
    print(f"  Decline turns              : {sum(1 for r in results if r['intent'] in ('explicit_decline', 'frustrated_decline'))}")
    print(f"  Constraint extractions     : {sum(len(r['constraints']) for r in results)}")
    print(f"  Conflicts flagged          : {sum(1 for r in results if r['conflicts_with_prior'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--call", type=Path, help="Path to JSON file of turns: [{role, text}, ...]")
    ap.add_argument("--model-id", help="Override Bedrock model id (default: settings.classifier_model_id)")
    ap.add_argument(
        "--predicted",
        type=Path,
        nargs="?",
        const=Path(__file__).parent / "_eloise_predicted_output.json",
        help="Use a JSON file of golden/expected outputs instead of calling Bedrock. "
        "With no value, uses the bundled _eloise_predicted_output.json.",
    )
    args = ap.parse_args()
    asyncio.run(run(args.call, args.model_id, args.predicted))


if __name__ == "__main__":
    sys.path.insert(0, "src")
    main()
