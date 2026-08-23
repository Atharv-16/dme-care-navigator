"""Coordinator working memory and post-call analysis (navigator only mutates at runtime)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from src.llm import chat_json
from src.models import CaseState, SupplierRecord, utc_now

from prompts.loader import load_persona

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
CONTEXT_MD = OUTPUT / "coordinator_context.md"

CallType = Literal["supplier", "clinic", "patient"]

GOODBYE_FROM_CALLER = "The caller is wrapping up. One-sentence goodbye, then [END]."
GOODBYE_FROM_CALLEE = "They are saying goodbye. One-sentence goodbye, then [END]."


def init_coordinator_context(case: CaseState) -> None:
    """Reset runtime coordinator memory at the start of each run."""
    case.coordinator_context = []
    OUTPUT.mkdir(parents=True, exist_ok=True)
    CONTEXT_MD.write_text(
        "# Coordinator working memory\n\n"
        "Append-only log of conclusions after each interaction. "
        "Static knowledge banks for all other parties stay unchanged.\n"
    )


def append_coordinator_context(case: CaseState, conclusion: str) -> None:
    if not conclusion.strip():
        return
    case.coordinator_context.append(conclusion.strip())
    with CONTEXT_MD.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {conclusion.strip()}\n")


def format_context_log(case: CaseState) -> str:
    if not case.coordinator_context:
        return "(empty — first interaction)"
    return "\n".join(f"- {line}" for line in case.coordinator_context)


def conclusion_from_supplier_call(
    case: CaseState,
    supplier: SupplierRecord,
    result: dict[str, Any],
) -> str:
    """Self-contained conclusion for simulate / fallback paths."""
    fields = result.get("fields") or {}
    leads = result.get("referral_leads") or []
    bits = [
        utc_now(),
        (
            f"Patient {case.patient.name} (DOB {case.patient.dob}), "
            f"{case.equipment.hcpcs} {case.equipment.description}, {case.patient.city}"
        ),
        f"Called {supplier.name} at {supplier.phone}",
    ]
    if fields.get("taking_new_medicare") is False:
        bits.append("Not taking new Medicare patients")
    elif fields.get("has_k0001") is False:
        bits.append("Takes Medicare but no K0001 in stock")
    elif fields.get("serves_area") is False:
        bits.append("Does not serve patient address")
    elif result.get("outcome") == "viable":
        eta = fields.get("delivery_eta_days")
        bits.append(
            f"Viable: Medicare yes, K0001 in stock, serves area"
            + (f", ETA ~{eta} business days after written order faxed" if eta else "")
        )
    elif result.get("outcome") == "voicemail":
        bits.append("Voicemail / no live answer")
    elif result.get("outcome") == "unclear":
        bits.append("Unclear — would not commit to ETA or stock")
    elif summary := result.get("summary"):
        bits.append(summary)

    for lead in leads:
        bits.append(f"Referral: {lead.get('name', '?')} at {lead.get('phone', '?')}")

    bits.append(f"Outcome: {result.get('outcome', 'unknown')}")

    if leads:
        lead = leads[0]
        bits.append(f"Next: call {lead.get('name', '?')} at {lead.get('phone', '?')}")
    elif result.get("outcome") == "viable":
        bits.append("Next: continue clinic chase for signed order, then fax to this supplier")
    elif result.get("outcome") in {"voicemail", "no_answer", "callback"}:
        bits.append(f"Next: retry {supplier.name} at {supplier.phone}")
    else:
        bits.append("Next: try next ranked supplier or follow referral")

    return " | ".join(bits)


def conclusion_from_clinic_call(case: CaseState, result: dict[str, Any]) -> str:
    """Self-contained conclusion for simulate / fallback paths."""
    bits = [
        utc_now(),
        f"Patient {case.patient.name}, {case.equipment.hcpcs}",
        f"Called {case.pcp.clinic} at {case.pcp.phone}, {case.pcp.name}",
    ]
    status = result.get("order_status") or result.get("outcome") or "unknown"
    if status == "in_queue":
        bits.append("Written order not signed; placed in Dr. Chen signature queue")
        bits.append("Outcome: in_queue")
        bits.append(f"Next: call {case.pcp.clinic} at {case.pcp.phone} in 2-3 days for signed K0001")
    elif status == "received":
        bits.append("Signed K0001 written order ready for navigator to collect")
        bits.append("Outcome: received")
        bits.append("Next: pick up order from clinic, continue viable supplier handoff")
    elif summary := result.get("summary"):
        bits.append(summary)
        bits.append(f"Outcome: {result.get('outcome', status)}")
        bits.append(f"Next: call {case.pcp.clinic} at {case.pcp.phone} as needed")
    else:
        bits.append(f"Outcome: {status}")
        bits.append(f"Next: call {case.pcp.clinic} at {case.pcp.phone}")

    return " | ".join(bits)


def _analyze_system_prompt() -> str:
    return load_persona("navigator_analyze")


def _case_snapshot(case: CaseState) -> str:
    return json.dumps(
        {
            "patient": case.patient.name,
            "equipment": case.equipment.hcpcs,
            "pcp_clinic": case.pcp.clinic,
            "pcp_order_status": case.pcp.order_status,
            "viable_suppliers": [s.name for s in case.suppliers if s.status == "viable"],
            "selected_supplier": case.selected_supplier_id,
        },
        indent=2,
    )


def _normalize(data: dict[str, Any]) -> dict[str, str]:
    """Coordinator always returns exactly reply + conclusion."""
    return {
        "reply": str(data.get("reply") or "").strip(),
        "conclusion": str(data.get("conclusion") or "").strip(),
    }


async def analyze_call(
    case: CaseState,
    *,
    call_type: CallType,
    transcript: list[dict[str, str]],
    counterpart_name: str,
    supplier: SupplierRecord | None = None,
) -> dict[str, str]:
    """Coordinator reads the full transcript; returns {reply, conclusion}."""
    user_payload: dict[str, Any] = {
        "call_type": call_type,
        "counterpart": counterpart_name,
        "timestamp": utc_now(),
        "case_snapshot": json.loads(_case_snapshot(case)),
        "working_memory": case.coordinator_context,
        "transcript": transcript,
    }
    if supplier is not None:
        user_payload["supplier_id"] = supplier.id

    raw = await chat_json(
        [
            {"role": "system", "content": _analyze_system_prompt()},
            {"role": "user", "content": json.dumps(user_payload, indent=2)},
        ]
    )
    result = _normalize(raw)
    if result["conclusion"]:
        append_coordinator_context(case, result["conclusion"])
    return result


async def compose_patient_reply(
    case: CaseState,
    *,
    intent: str,
) -> dict[str, str]:
    """Coordinator drafts patient TTS text; returns {reply, conclusion}."""
    user_payload = {
        "call_type": "patient",
        "intent": intent,
        "timestamp": utc_now(),
        "case_snapshot": json.loads(_case_snapshot(case)),
        "working_memory": case.coordinator_context,
    }
    raw = await chat_json(
        [
            {"role": "system", "content": _analyze_system_prompt()},
            {"role": "user", "content": json.dumps(user_payload, indent=2)},
        ]
    )
    result = _normalize(raw)
    if not result["reply"]:
        result["reply"] = intent
    if result["conclusion"]:
        append_coordinator_context(case, result["conclusion"])
    return result
