"""Deterministic simulated workers for offline e2e (no LLM)."""

from __future__ import annotations

from src.models import (
    CaseState,
    PCPCallResult,
    ReferralLead,
    SupplierCallResult,
    SupplierRecord,
    WrittenOrder,
    utc_now,
)


async def sim_call_supplier(case: CaseState, supplier: SupplierRecord) -> SupplierCallResult:
    supplier.status = "calling"
    supplier.call_attempts += 1
    supplier.last_contact_at = utc_now()
    gt = supplier.ground_truth or {}
    behavior = gt.get("behavior")

    transcript = [
        {
            "speaker": "advocate",
            "text": f"Hi, calling about a Medicare K0001 for {case.patient.name} in Chicago.",
        }
    ]

    if behavior == "no_answer":
        transcript.append(
            {"speaker": "callee", "text": "Voicemail. [NO_ANSWER]"},
        )
        return SupplierCallResult(
            supplier_id=supplier.id,
            outcome="voicemail",
            fields={"responsive": False},
            summary="No answer / voicemail",
            confidence=1.0,
            transcript=transcript,
        )

    if behavior == "decline_not_taking_new":
        transcript.append(
            {
                "speaker": "callee",
                "text": "We're not taking new Medicare patients. Try Lakeshore DME Partners at 312-555-0288.",
            }
        )
        ref = gt.get("referral") or {}
        return SupplierCallResult(
            supplier_id=supplier.id,
            outcome="rejected",
            fields={"taking_new_medicare": False, "has_k0001": True, "serves_area": True},
            referral_leads=[
                ReferralLead(
                    name=ref.get("name", "Lakeshore DME Partners"),
                    phone=ref.get("phone", "(312) 555-0288"),
                    note=ref.get("note"),
                    source_supplier_id=supplier.id,
                )
            ],
            summary="Not taking new Medicare; referred Lakeshore",
            confidence=1.0,
            transcript=transcript,
        )

    if behavior == "no_stock":
        transcript.append(
            {"speaker": "callee", "text": "We take Medicare but we're out of K0001 manuals."}
        )
        return SupplierCallResult(
            supplier_id=supplier.id,
            outcome="rejected",
            fields={"taking_new_medicare": True, "has_k0001": False, "serves_area": True},
            summary="Out of stock on K0001",
            confidence=1.0,
            transcript=transcript,
        )

    if behavior == "outside_service_area":
        ref = gt.get("referral") or {}
        transcript.append(
            {
                "speaker": "callee",
                "text": f"We have manuals but don't deliver there. Try {ref.get('name')}.",
            }
        )
        leads = []
        if ref:
            leads.append(
                ReferralLead(
                    name=ref["name"],
                    phone=ref["phone"],
                    note=ref.get("note"),
                    source_supplier_id=supplier.id,
                )
            )
        return SupplierCallResult(
            supplier_id=supplier.id,
            outcome="rejected",
            fields={"taking_new_medicare": True, "has_k0001": True, "serves_area": False},
            referral_leads=leads,
            summary="Outside service area",
            confidence=1.0,
            transcript=transcript,
        )

    if behavior == "says_yes_then_evasive":
        transcript.append(
            {
                "speaker": "callee",
                "text": "Sure we can help... uh, warehouse is checking, someone will call you back.",
            }
        )
        return SupplierCallResult(
            supplier_id=supplier.id,
            outcome="unclear",
            fields={
                "taking_new_medicare": True,
                "has_k0001": True,
                "serves_area": True,
                "delivery_eta_days": None,
            },
            summary="Sounded positive but vague / call back — unreliable",
            confidence=0.6,
            transcript=transcript,
        )

    # viable (including referral leads with viable ground truth)
    eta = gt.get("delivery_eta_days") or 5
    transcript.append(
        {
            "speaker": "callee",
            "text": f"Yes — Original Medicare, K0001 in stock, Chicago delivery in about {eta} days once we have the written order.",
        }
    )
    return SupplierCallResult(
        supplier_id=supplier.id,
        outcome="viable",
        fields={
            "taking_new_medicare": True,
            "has_k0001": True,
            "serves_area": True,
            "delivery_eta_days": eta,
            "responsive": True,
        },
        summary=f"Viable supplier; ETA ~{eta} days",
        confidence=1.0,
        transcript=transcript,
    )


async def sim_call_pcp(case: CaseState) -> PCPCallResult:
    transcript = [
        {
            "speaker": "advocate",
            "text": f"Following up on written order for {case.patient.name}, K0001 wheelchair.",
        }
    ]
    if case.pcp.chase_attempts == 0:
        transcript.append(
            {
                "speaker": "callee",
                "text": "Looks like it never got to the nurse. I'll put it in Dr. Chen's signature queue today.",
            }
        )
        return PCPCallResult(
            outcome="in_queue",
            order_status="in_queue",
            summary="Order routed to signature queue",
            confidence=1.0,
            transcript=transcript,
        )

    transcript.append(
        {
            "speaker": "callee",
            "text": "Written order for standard manual wheelchair K0001 is signed and ready to fax.",
        }
    )
    return PCPCallResult(
        outcome="received",
        order_status="received",
        order=WrittenOrder(
            signed=True,
            signed_at=utc_now()[:10],
            equipment_text="standard manual wheelchair",
            hcpcs=case.equipment.hcpcs,
            matches_request=True,
            source="sunrise_family_medicine",
        ),
        summary="Signed written order ready",
        confidence=1.0,
        transcript=transcript,
    )
