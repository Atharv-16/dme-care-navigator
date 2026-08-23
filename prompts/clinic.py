import json
from typing import Any


def clinic_touch_rule(*, touches: int) -> str:
    if touches == 0:
        return (
            "FIRST CALL: the written order is NOT signed. Put it in Dr. Chen's "
            "signature queue. Do not say it is ready."
        )
    return (
        "FOLLOW-UP CALL: Dr. Chen HAS signed the K0001 written order. "
        "Tell the navigator it is ready to collect. Do not repeat the first-call story."
    )


def clinic_self_system(
    *,
    clinic_name: str,
    doctor_name: str,
    ground_truth: dict[str, Any],
    touches: int,
    persona: str = "",
    actor_notes: str = "",
) -> str:
    parts = [
        f"You are front desk at {clinic_name} for {doctor_name}.",
    ]
    if persona:
        parts.append(persona)
    parts.extend(
        [
            f"Ground truth: {json.dumps(ground_truth)}. Touches so far: {touches}.",
            clinic_touch_rule(touches=touches),
        ]
    )
    if actor_notes:
        parts.append(f"Actor guidance: {actor_notes}")
    parts.append(
        "This call is ONLY with the CARE NAVIGATOR. "
        "Your job is the written order: unsigned vs in Dr. Chen's signature queue vs signed. "
        "FORBIDDEN: contacting DME suppliers, waiting on a supplier reply, tracking "
        "supplier status, ship dates, or carriers. You do NOT route orders to shops. "
        "When signed, you give the order to the NAVIGATOR; they take it to the supplier. "
        "If asked about a supplier say: we don't work with the supplier; the navigator does. "
        "Short replies. Include [END] when done."
    )
    return " ".join(parts)


def clinic_opener(*, clinic_name: str) -> str:
    return (
        f"The phone rang at {clinic_name}. Pick up. "
        "Greeting only: clinic name and how can I help. Do not mention the order yet."
    )
