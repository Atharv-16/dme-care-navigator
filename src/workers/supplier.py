from __future__ import annotations

import json

from src.models import CaseState, ReferralLead, SupplierCallResult, SupplierRecord, utc_now
from src.workers.phone import extract_structured_result, run_voice_call

SUPPLIER_RESULT_SCHEMA = """
{
  "outcome": "viable|rejected|callback|no_answer|voicemail|unclear",
  "fields": {
    "taking_new_medicare": true|false|null,
    "has_k0001": true|false|null,
    "serves_area": true|false|null,
    "delivery_eta_days": number|null,
    "responsive": true|false|null
  },
  "referral_leads": [{"name": str, "phone": str, "note": str|null}],
  "callback_at": str|null,
  "summary": str,
  "confidence": 0-1,
  "needs_human": bool
}
Viable means: taking new Medicare AND has K0001 AND serves area AND gave a usable path to delivery.
Rejected means clearly cannot serve this request.
no_answer/voicemail if nobody substantive answered.
"""


def _agent_prompt(case: CaseState, supplier: SupplierRecord) -> str:
    return f"""
You are a care-navigation VOICE agent calling a DME supplier on behalf of a patient.
This is a live phone call. Be concise, professional, warm. Ask cross-questions when vague.

GOAL: Learn whether this supplier can deliver a standard manual wheelchair (HCPCS K0001)
for the patient under Original Medicare Part B.

PATIENT (min info — do not dump full Medicare ID unless they require it for a callback ticket):
- Name: {case.patient.name}
- City: {case.patient.city}
- Plan: {case.patient.plan} (no Medigap)
- Equipment: {case.equipment.description} ({case.equipment.hcpcs})

SUPPLIER ON THE LINE:
- {supplier.name}
- {supplier.phone}
- {supplier.address}

MUST LEARN:
1) Taking new Original Medicare patients?
2) Stock a standard manual wheelchair (K0001)?
3) Deliver to patient's Chicago address / service area?
4) Rough delivery ETA once written order is in hand?

ALLOWED:
- Clarify fuzzy answers
- Ask who else they'd recommend if they cannot help (capture name + phone)
- Schedule a callback if they ask

MUST NOT:
- Invent Medicare approval or claim payment amounts beyond "~patient typically owes Part B coinsurance (~20%) after deductible"
- Pretend you already have a signed written order if PCP order is not received yet
  (current PCP order status: {case.pcp.order_status})
- Share full MBI unless required; prefer offering a callback with demographics

When done, thank them and include [END_CALL].
""".strip()


def _callee_prompt(supplier: SupplierRecord) -> str:
    gt = supplier.ground_truth or {}
    return f"""
You are the person who answered the phone at a DME supplier.
Stay in character. Short phone-style replies. Do not break character or mention AI.

BUSINESS: {supplier.name}
PHONE: {supplier.phone}
ADDRESS: {supplier.address}

YOUR GROUND TRUTH (follow this; do not volunteer everything at once — answer what is asked,
but do not contradict these facts):
{json.dumps(gt, indent=2)}

If behavior is no_answer / voicemail: respond once as a voicemail greeting and include [NO_ANSWER].
If you cannot help and ground_truth includes a referral, share it when asked or when declining.
When the conversation is naturally over you may include [END_CALL].
""".strip()


async def call_supplier(case: CaseState, supplier: SupplierRecord) -> SupplierCallResult:
    supplier.status = "calling"
    supplier.call_attempts += 1
    supplier.last_contact_at = utc_now()

    gt = supplier.ground_truth or {}
    behavior = gt.get("behavior")

    # Deterministic short-circuit for pure no-answer so demos stay crisp
    if behavior == "no_answer" and supplier.call_attempts == 1:
        transcript = [
            {
                "speaker": "callee",
                "text": (
                    f"You've reached {supplier.name}. We're unable to take your call. "
                    "Please leave a message after the tone. [NO_ANSWER]"
                ),
            },
            {
                "speaker": "advocate",
                "text": "I'll try again another time. [END_CALL]",
            },
        ]
        return SupplierCallResult(
            supplier_id=supplier.id,
            outcome="voicemail",
            fields={"responsive": False},
            summary="Voicemail / no live answer on first attempt.",
            confidence=0.95,
            transcript=transcript,
        )

    transcript = await run_voice_call(
        agent_system=_agent_prompt(case, supplier),
        callee_system=_callee_prompt(supplier),
        opening_line="hello",
        max_turns=8,
    )

    raw = await extract_structured_result(
        schema_hint=SUPPLIER_RESULT_SCHEMA,
        transcript=transcript,
        extra_context=f"supplier_id={supplier.id}",
    )

    leads = []
    for item in raw.get("referral_leads") or []:
        if not item:
            continue
        leads.append(
            ReferralLead(
                name=item.get("name") or "Unknown",
                phone=item.get("phone") or "n/a",
                note=item.get("note"),
                source_supplier_id=supplier.id,
            )
        )

    # Light guardrails: if extractor says viable, verify field conjunction
    fields = raw.get("fields") or {}
    outcome = raw.get("outcome") or "unclear"
    if outcome == "viable":
        if not (
            fields.get("taking_new_medicare") is True
            and fields.get("has_k0001") is True
            and fields.get("serves_area") is True
        ):
            outcome = "unclear"

    return SupplierCallResult(
        supplier_id=supplier.id,
        outcome=outcome,  # type: ignore[arg-type]
        fields=fields,
        referral_leads=leads,
        callback_at=raw.get("callback_at"),
        summary=raw.get("summary") or "No summary",
        confidence=float(raw.get("confidence") or 0.5),
        needs_human=bool(raw.get("needs_human")),
        transcript=transcript,
    )
