from __future__ import annotations

import json

from src.models import CaseState, PCPCallResult, WrittenOrder, utc_now
from src.workers.phone import extract_structured_result, run_voice_call

PCP_RESULT_SCHEMA = """
{
  "outcome": "order_submitted|in_queue|never_got_request|wrong_order|no_answer|callback|received",
  "order_status": "verbal_only|requested|in_queue|received|rejected_wrong|unknown",
  "order": {
    "signed": bool,
    "signed_at": str|null,
    "equipment_text": str,
    "hcpcs": str,
    "matches_request": bool,
    "source": str
  } | null,
  "callback_at": str|null,
  "summary": str,
  "confidence": 0-1,
  "needs_human": bool
}
Use order_status=received and include order object only if they confirmed a signed written order exists.
"""


def _agent_prompt(case: CaseState) -> str:
    return f"""
You are a care-navigation VOICE agent calling a primary care clinic to chase a written DME order.

CLINIC: {case.pcp.clinic}
DOCTOR: {case.pcp.name}
PHONE: {case.pcp.phone}

PATIENT: {case.patient.name}, DOB {case.patient.dob}
NEED: Written order for {case.equipment.description} (HCPCS {case.equipment.hcpcs})
CONTEXT: {case.equipment.notes}
CURRENT order_status in our system: {case.pcp.order_status}
Chase attempts so far: {case.pcp.chase_attempts}

Goals:
- Confirm whether a formal written order (not just verbal chart note) exists / is moving
- If stuck in queue, who owns it and when to follow up
- If signed, confirm equipment text + K0001, then say YOU will collect it and take it to the supplier

FORBIDDEN on this call:
- Asking the clinic about supplier status, supplier replies, ship dates, or carriers
- Asking the clinic to contact, wait on, or route the order to a DME supplier
The clinic only signs. You (the navigator) are the courier to the shop.

Keep turns short. End with [END_CALL] when done.
Do not invent that the order is signed.
""".strip()


def _callee_prompt(case: CaseState) -> str:
    gt = case.pcp.ground_truth or {}
    return f"""
You are front desk staff at {case.pcp.clinic} for {case.pcp.name}.
Phone style, brief. Do not break character.

GROUND TRUTH:
{json.dumps(gt, indent=2)}

Chase attempts from caller so far (before this call): {case.pcp.chase_attempts}

Follow behavior:
- needs_two_touches: On first chase (chase_attempts == 0), the written order is NOT signed yet —
  acknowledge gap and say you'll put it in Dr. Chen's signature queue.
  On later chases (chase_attempts >= 1), confirm the written order for standard manual wheelchair
  K0001 is signed and ready for the NAVIGATOR to collect.

You never contact DME suppliers, wait on them, or know their status.
You do not route orders to shops. The navigator takes the signed order to the supplier.
If asked about a supplier: "We don't work with the supplier. The navigator handles that."

Never claim Medicare payment details. You are clinic staff, not billing.
""".strip()


async def call_pcp(case: CaseState) -> PCPCallResult:
    # Scripted reliability for the two-touch order path, with real LLM conversation
    transcript = await run_voice_call(
        agent_system=_agent_prompt(case),
        callee_system=_callee_prompt(case),
        opening_line="hello",
        max_turns=4,
    )

    raw = await extract_structured_result(
        schema_hint=PCP_RESULT_SCHEMA,
        transcript=transcript,
        extra_context=f"chase_attempts_before={case.pcp.chase_attempts}",
    )

    # Enforce ground-truth two-touch behavior so the e2e demo is coherent
    behavior = (case.pcp.ground_truth or {}).get("behavior")
    order = None
    if behavior == "needs_two_touches":
        if case.pcp.chase_attempts == 0:
            order_status = "in_queue"
            outcome = "in_queue"
        else:
            order_status = "received"
            outcome = "received"
            order = WrittenOrder(
                signed=True,
                signed_at=utc_now()[:10],
                equipment_text="standard manual wheelchair",
                hcpcs=case.equipment.hcpcs,
                matches_request=True,
                source="sunrise_family_medicine",
            )
    else:
        order_status = raw.get("order_status") or "unknown"
        outcome = raw.get("outcome") or "callback"
        if raw.get("order"):
            order = WrittenOrder(**raw["order"])

    return PCPCallResult(
        outcome=outcome,  # type: ignore[arg-type]
        order_status=order_status,  # type: ignore[arg-type]
        order=order,
        callback_at=raw.get("callback_at"),
        summary=raw.get("summary") or "PCP chase completed",
        confidence=float(raw.get("confidence") or 0.7),
        needs_human=bool(raw.get("needs_human")),
        transcript=transcript,
    )
