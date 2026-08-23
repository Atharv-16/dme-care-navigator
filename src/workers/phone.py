from __future__ import annotations

import json
from typing import Any

from src.llm import chat_json, chat_text


async def run_voice_call(
    *,
    agent_system: str,
    callee_system: str,
    opening_line: str,
    max_turns: int = 8,
    force_end_phrases: tuple[str, ...] = ("[END_CALL]", "[HANGUP]"),
) -> list[dict[str, str]]:
    """
    Simulated phone channel: care-advocate voice agent <-> callee persona.

    MOCKED: PSTN / Twilio. REAL: LLM turns for both sides (agentic conversation).
    """
    transcript: list[dict[str, str]] = []
    messages_agent: list[dict[str, str]] = [
        {"role": "system", "content": agent_system},
        {
            "role": "user",
            "content": (
                "The call just connected. Speak your opening line naturally as if on a phone. "
                "Keep turns short (1-3 sentences). Ask clarifying / cross questions when answers "
                "are vague. When you have what you need OR the other party clearly cannot help, "
                "end by saying thanks and include the token [END_CALL]."
            ),
        },
    ]
    messages_callee: list[dict[str, str]] = [
        {"role": "system", "content": callee_system},
    ]

    # Agent opens
    agent_line = await chat_text(messages_agent, temperature=0.5)
    transcript.append({"speaker": "advocate", "text": agent_line})
    messages_agent.append({"role": "assistant", "content": agent_line})
    messages_callee.append({"role": "user", "content": agent_line})

    for _ in range(max_turns - 1):
        if any(tok in agent_line for tok in force_end_phrases):
            break

        callee_line = await chat_text(messages_callee, temperature=0.55)
        transcript.append({"speaker": "callee", "text": callee_line})
        messages_callee.append({"role": "assistant", "content": callee_line})
        messages_agent.append({"role": "user", "content": callee_line})

        if any(tok in callee_line for tok in force_end_phrases):
            break
        # Voicemail / no-answer short-circuit
        low = callee_line.lower()
        if "voicemail" in low or "leave a message" in low or "[no_answer]" in low:
            transcript.append(
                {
                    "speaker": "advocate",
                    "text": "Okay, I'll try again later. [END_CALL]",
                }
            )
            break

        agent_line = await chat_text(messages_agent, temperature=0.45)
        transcript.append({"speaker": "advocate", "text": agent_line})
        messages_agent.append({"role": "assistant", "content": agent_line})
        messages_callee.append({"role": "user", "content": agent_line})

        if any(tok in agent_line for tok in force_end_phrases):
            break

    return transcript


async def extract_structured_result(
    *,
    schema_hint: str,
    transcript: list[dict[str, str]],
    extra_context: str = "",
) -> dict[str, Any]:
    payload = {
        "transcript": transcript,
        "extra_context": extra_context,
    }
    data = await chat_json(
        [
            {
                "role": "system",
                "content": (
                    "You extract structured outcomes from a care-navigation phone call transcript. "
                    "Return ONLY JSON matching the requested schema. Do not invent facts that were "
                    "not stated or strongly implied. If unknown, use null / unclear outcomes.\n\n"
                    f"Schema:\n{schema_hint}"
                ),
            },
            {"role": "user", "content": json.dumps(payload)},
        ],
        temperature=0.1,
    )
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object from extractor")
    return data
