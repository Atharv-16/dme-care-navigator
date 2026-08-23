import json
from typing import Any


BASE_SUPPLIER_RULES = (
    "Short phone-style replies. "
    "The caller is a CARE NAVIGATOR, not the patient. "
    "Do NOT offer to call the patient's doctor, clinic, or PCP. "
    "You do not chase written orders. If they ask about the doctor, say: "
    "the navigator handles the PCP; you only need the faxed order later. "
    "You MAY refer another supplier name/phone if you cannot help. "
    "If behavior is no_answer, voicemail once and [NO_ANSWER]."
)


def supplier_self_system(
    *,
    supplier_name: str,
    knowledge: dict[str, Any],
    persona: str = "",
    actor_notes: str = "",
) -> str:
    parts = [f"You work the phone at DME supplier {supplier_name}."]
    if persona:
        parts.append(persona)
    parts.append(f"Follow ground truth exactly: {json.dumps(knowledge)}.")
    if actor_notes:
        parts.append(f"Actor guidance: {actor_notes}")
    parts.append(BASE_SUPPLIER_RULES)
    return " ".join(parts)


def supplier_opener(*, supplier_name: str) -> str:
    return (
        f"The phone rang at {supplier_name}. Pick up. "
        "Greeting only: say the shop name and ask how you can help. "
        "Do not mention wheelchairs, Medicare, stock, or ETAs until the caller asks."
    )
