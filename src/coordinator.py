"""Coordinator working memory and post-call analysis (navigator only mutates at runtime)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field
from tenacity import stop_after_attempt, wait_fixed

from src.llm import chat_json, chat_text
from src.models import (
    CallMemoryRecord,
    CallPlan,
    CaseState,
    OrderStatus,
    ReferralLead,
    SupplierRecord,
    WrittenOrder,
    utc_now,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
CONTEXT_MD = OUTPUT / "coordinator_context.md"

CallType = Literal["supplier", "clinic", "patient", "medicare"]

GOODBYE_FROM_CALLER = "The caller is wrapping up. One-sentence goodbye, then [END]."
GOODBYE_FROM_CALLEE = "They are saying goodbye. One-sentence goodbye, then [END]."


class StatePatch(BaseModel):
    order_status: OrderStatus | None = None
    order: WrittenOrder | None = None
    supplier_id: str | None = None
    supplier_outcome: str | None = None
    supplier_fields: dict[str, Any] = Field(default_factory=dict)
    referral_leads: list[ReferralLead] = Field(default_factory=list)
    patient_coinsurance_explained: bool | None = None
    delivery_status: str | None = None


class PostCallUpdate(BaseModel):
    summary: str
    verified_facts: list[str] = Field(default_factory=list)
    outcome: str = "unknown"
    state_patch: StatePatch = Field(default_factory=StatePatch)
    next_call: CallPlan = Field(default_factory=CallPlan)
    reply: str = ""
    conclusion: str = ""


def canonical_context(
    case: CaseState,
    *,
    current_call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One validated source of truth injected into planner and live-call prompts."""
    return {
        "case": {
            "case_id": case.case_id,
            "goal": case.goal,
            "status": case.status,
            "patient": {
                "name": case.patient.name,
                "dob": case.patient.dob,
                "phone": case.patient.phone,
                "address": case.patient.address,
                "city": case.patient.city,
                "plan": case.patient.plan,
                "supplemental": case.patient.supplemental,
            },
            "request": {
                "hcpcs": case.equipment.hcpcs,
                "description": case.equipment.description,
            },
            "clinic": {
                "name": case.pcp.clinic,
                "doctor": case.pcp.name,
                "phone": case.pcp.phone,
            },
        },
        "workflow": {
            "order_status": case.pcp.order_status,
            "order": case.pcp.order.model_dump(mode="json") if case.pcp.order else None,
            "selected_supplier_id": case.selected_supplier_id,
            "delivery": case.delivery.model_dump(mode="json"),
            "patient_notified": case.patient.coinsurance_explained,
            "suppliers": [
                {
                    "id": supplier.id,
                    "name": supplier.name,
                    "phone": supplier.phone,
                    "status": supplier.status,
                    "taking_new_medicare": supplier.taking_new_medicare,
                    "has_k0001": supplier.has_k0001,
                    "serves_area": supplier.serves_area,
                    "delivery_eta_days": supplier.delivery_eta_days,
                }
                for supplier in case.suppliers
            ],
        },
        "memory": [
            record.model_dump(mode="json") for record in case.call_memory[-12:]
        ],
        "current_call": current_call,
    }


def live_context_json(
    case: CaseState,
    *,
    target_id: str,
    target_name: str,
    call_type: str,
    goal: str,
    facts_to_share: list[str],
    questions: list[str],
) -> str:
    active_plan = case.known_facts.get("_active_call_plan")
    if isinstance(active_plan, dict) and active_plan.get("target_id") in {
        None,
        target_id,
    }:
        goal = str(active_plan.get("goal") or goal)
        facts_to_share = list(active_plan.get("facts_to_share") or facts_to_share)
        questions = list(active_plan.get("questions") or questions)
    context = canonical_context(
        case,
        current_call={
            "target_id": target_id,
            "target_name": target_name,
            "call_type": call_type,
            "goal": goal,
            "facts_to_share": facts_to_share,
            "questions": questions,
        },
    )
    patient = context["case"]["patient"]
    if call_type == "supplier":
        for key in ("dob", "phone", "address"):
            patient.pop(key, None)
        context["workflow"]["suppliers"] = [
            supplier
            for supplier in context["workflow"]["suppliers"]
            if supplier["id"] == target_id
        ]
        context["memory"] = []
    elif call_type == "clinic":
        patient.pop("plan", None)
        patient.pop("supplemental", None)
        context["workflow"]["suppliers"] = []
        context["memory"] = []
    elif call_type == "medicare":
        patient.pop("phone", None)
        patient.pop("address", None)
        context["workflow"]["suppliers"] = [
            supplier
            for supplier in context["workflow"]["suppliers"]
            if supplier["id"] == case.selected_supplier_id
        ]
    return json.dumps(context, indent=2)


def persist_context(case: CaseState) -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"{case.case_id}.context.json"
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(canonical_context(case), indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def persist_call_transcript(
    case: CaseState,
    *,
    record: CallMemoryRecord,
    transcript: list[dict[str, str]],
) -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"{case.case_id}.calls.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "call": record.model_dump(mode="json"),
                    "transcript": transcript,
                }
            )
            + "\n"
        )
    return path


def init_coordinator_context(case: CaseState) -> None:
    """Reset runtime coordinator memory at the start of each run."""
    case.coordinator_context = []
    case.call_memory = []
    case.next_call_plan = None
    OUTPUT.mkdir(parents=True, exist_ok=True)
    CONTEXT_MD.write_text(
        "# Coordinator working memory\n\n"
        "Append-only log of conclusions after each interaction. "
        "Static knowledge banks for all other parties stay unchanged.\n"
    )
    (OUTPUT / f"{case.case_id}.calls.jsonl").write_text("", encoding="utf-8")
    persist_context(case)


def append_coordinator_context(case: CaseState, conclusion: str) -> None:
    if not conclusion.strip():
        return
    case.coordinator_context.append(conclusion.strip())
    with CONTEXT_MD.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {conclusion.strip()}\n")
    persist_context(case)


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
    from prompts.loader import build_system_prompt

    return build_system_prompt("navigator", mode="analyze")


def _patient_compose_system_prompt() -> str:
    from prompts.loader import build_system_prompt

    return (
        build_system_prompt("navigator")
        + "\n\nDraft one brief English phone update for the patient. "
        'Return only JSON: {"reply": "spoken message", "conclusion": "audit line"}.'
    )


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
    """Every party returns exactly reply + conclusion."""
    return {
        "reply": str(data.get("reply") or "").strip(),
        "conclusion": str(data.get("conclusion") or "").strip(),
    }


def _clinic_confirmed_order_ready(transcript: list[dict[str, str]]) -> bool:
    """Recognize explicit clinic readiness so routing never depends on an LLM guess."""
    ready: bool | None = None
    for turn in transcript:
        if turn.get("speaker") == "navigator":
            continue
        text = str(turn.get("text") or "").lower()
        if re.search(
            r"\b(not|isn't|isnt|wasn't|wasnt)\s+(signed|ready)\b"
            r"|\bnot yet\b|\bstill (in|the) queue\b|\bwaiting (for|on).{0,20}signature\b",
            text,
        ) and not re.search(r"\b(now|already)\s+ready\b", text):
            ready = False
            continue
        if re.search(
            r"\bready\s+(to\s+(collect|pick\s*up)|for\s+(collection|pickup))\b"
            r"|\b(signed\s+)?(written\s+)?order\s+(is|'s)\s+ready\b"
            r"|\b(now|already)\s+ready\b",
            text,
        ):
            ready = True
    return ready is True


async def json_turn(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
) -> str:
    """One spoken turn. Navigator may send {reply, conclusion}; others send {reply}. TTS uses reply."""
    try:
        raw = await chat_json(messages, temperature=temperature)
        if isinstance(raw, dict):
            reply = str(raw.get("reply") or "").strip()
            if reply:
                return reply
        text = str(raw).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    return (await chat_text(messages, temperature=temperature)).strip()


async def analyze_call(
    case: CaseState,
    *,
    call_type: CallType,
    transcript: list[dict[str, str]],
    counterpart_name: str,
    supplier: SupplierRecord | None = None,
    status_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Extract a validated state patch and next-call plan from one transcript."""
    user_payload: dict[str, Any] = {
        "call_type": call_type,
        "counterpart": counterpart_name,
        "timestamp": utc_now(),
        "context": canonical_context(case),
        "transcript": transcript,
    }
    if supplier is not None:
        user_payload["supplier_id"] = supplier.id

    async def notify(state: str, **details: Any) -> None:
        if status_callback is None:
            return
        result = status_callback({"state": state, **details})
        if inspect.isawaitable(result):
            await result

    messages = [
        {"role": "system", "content": _analyze_system_prompt()},
        {"role": "user", "content": json.dumps(user_payload, indent=2)},
    ]
    primary_model = os.getenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    fallback_model = os.getenv(
        "GEMINI_ANALYSIS_FALLBACK_MODEL",
        "gemini-flash-lite-latest",
    )
    models = list(dict.fromkeys((primary_model, fallback_model)))
    max_attempts = 2
    raw: Any = None

    for index, model in enumerate(models):
        def before_retry(retry_state: Any, *, current_model: str = model) -> None:
            exception = retry_state.outcome.exception() if retry_state.outcome else None
            error = type(exception).__name__ if exception else "temporary error"
            delay = float(getattr(retry_state.next_action, "sleep", 0) or 0)
            asyncio.create_task(
                notify(
                    "retry",
                    attempt=retry_state.attempt_number + 1,
                    max_attempts=max_attempts,
                    delay_seconds=delay,
                    error=error,
                    model=current_model,
                )
            )

        await notify("sent", model=model)
        await notify("running", model=model)
        request = chat_json.retry_with(
            wait=wait_fixed(float(os.getenv("LLM_RETRY_DELAY_SEC", "10"))),
            stop=stop_after_attempt(max_attempts),
            before_sleep=before_retry,
        )
        try:
            raw = await request(messages, model=model)
            await notify("received", model=model)
            break
        except Exception as exc:
            if index + 1 < len(models):
                await notify(
                    "fallback",
                    error=type(exc).__name__,
                    from_model=model,
                    to_model=models[index + 1],
                )
                continue
            await notify("failed", error=type(exc).__name__, model=model)
            raise
    update = PostCallUpdate.model_validate(raw)
    if call_type == "clinic" and _clinic_confirmed_order_ready(transcript):
        update.outcome = "received"
        update.summary = (
            "Clinic explicitly confirmed the signed K0001 written order is ready "
            "for collection."
        )
        fact = "Signed K0001 written order is ready for collection from the clinic"
        if fact not in update.verified_facts:
            update.verified_facts.append(fact)
        update.state_patch.order_status = "received"
        update.state_patch.order = WrittenOrder(
            signed=True,
            equipment_text=case.equipment.description,
            hcpcs=case.equipment.hcpcs,
            matches_request=True,
            source=case.pcp.clinic,
        )
    if call_type != "clinic":
        update.state_patch.order_status = None
        update.state_patch.order = None
    if call_type == "supplier" and supplier is not None:
        update.state_patch.supplier_id = supplier.id
    else:
        update.state_patch.supplier_id = None
        update.state_patch.supplier_outcome = None
        update.state_patch.supplier_fields = {}
        update.state_patch.referral_leads = []
    call_id = f"call_{len(case.call_memory) + 1:03d}"
    party_id = supplier.id if supplier is not None else call_type
    record = CallMemoryRecord(
        call_id=call_id,
        party_id=party_id,
        party_name=counterpart_name,
        call_type=call_type,
        summary=update.summary,
        verified_facts=update.verified_facts,
        outcome=update.outcome,
    )
    case.call_memory.append(record)
    persist_call_transcript(case, record=record, transcript=transcript)
    case.next_call_plan = update.next_call if update.next_call.action != "none" else None
    conclusion = update.conclusion or (
        f"{record.at} | {counterpart_name} | {update.summary} | "
        f"Outcome: {update.outcome} | Next: {update.next_call.action}"
    )
    append_coordinator_context(case, conclusion)
    persist_context(case)
    return update.model_dump(mode="json")


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
        "context": canonical_context(case),
    }
    raw = await chat_json(
        [
            {"role": "system", "content": _patient_compose_system_prompt()},
            {"role": "user", "content": json.dumps(user_payload, indent=2)},
        ]
    )
    result = _normalize(raw)
    if not result["reply"]:
        result["reply"] = intent
    if result["conclusion"]:
        append_coordinator_context(case, result["conclusion"])
    return result
