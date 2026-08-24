from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SupplierStatus = Literal[
    "not_contacted",
    "calling",
    "awaiting_callback",
    "viable",
    "rejected",
    "unresponsive",
    "selected",
]

OrderStatus = Literal[
    "verbal_only",
    "requested",
    "in_queue",
    "received",
    "rejected_wrong",
    "unknown",
]

CaseStatus = Literal[
    "intake",
    "in_progress",
    "waiting",
    "ready_for_handoff",
    "completed",
    "escalated",
]


class ReferralLead(BaseModel):
    name: str
    phone: str
    note: str | None = None
    source_supplier_id: str | None = None


class WrittenOrder(BaseModel):
    signed: bool = True
    signed_at: str | None = None
    equipment_text: str = "standard manual wheelchair"
    hcpcs: str = "K0001"
    matches_request: bool = True
    source: str = "clinic"


class SupplierRecord(BaseModel):
    id: str
    name: str
    phone: str
    address: str
    priority_score: float = 0.0
    status: SupplierStatus = "not_contacted"
    taking_new_medicare: bool | None = None
    has_k0001: bool | None = None
    delivery_eta_days: int | None = None
    serves_area: bool | None = None
    responsive: bool | None = None
    referral_leads: list[ReferralLead] = Field(default_factory=list)
    call_attempts: int = 0
    last_contact_at: str | None = None
    next_followup_at: str | None = None
    transcript_summary: str | None = None
    notes: list[str] = Field(default_factory=list)
    ground_truth: dict[str, Any] = Field(default_factory=dict)
    from_referral: bool = False


class Patient(BaseModel):
    name: str
    dob: str
    age: int | None = None
    phone: str
    address: str
    city: str
    medicare_mbid: str
    plan: str
    supplemental: bool = False
    coinsurance_explained: bool = False
    reachable: bool = True


class Equipment(BaseModel):
    type: str
    hcpcs: str
    description: str
    notes: str | None = None


class PCPState(BaseModel):
    name: str
    clinic: str
    phone: str
    city: str
    order_status: OrderStatus = "verbal_only"
    order: WrittenOrder | None = None
    last_contact_at: str | None = None
    next_followup_at: str | None = None
    chase_attempts: int = 0
    notes: list[str] = Field(default_factory=list)
    ground_truth: dict[str, Any] = Field(default_factory=dict)


class Delivery(BaseModel):
    status: str = "not_scheduled"
    scheduled_for: str | None = None
    notes: str | None = None


class Escalation(BaseModel):
    reason: str
    summary: str
    recommended_human_action: str
    at: str = Field(default_factory=utc_now)


class PatientUpdate(BaseModel):
    at: str = Field(default_factory=utc_now)
    message: str
    channel: str = "phone_mock"


class EventLogEntry(BaseModel):
    at: str = Field(default_factory=utc_now)
    actor: str
    action: str
    detail: str


PlannerAction = Literal[
    "call_clinic",
    "call_supplier",
    "call_patient",
    "call_medicare",
    "handoff",
    "complete",
    "escalate",
    "none",
]


class CallMemoryRecord(BaseModel):
    call_id: str
    at: str = Field(default_factory=utc_now)
    party_id: str
    party_name: str
    call_type: Literal["supplier", "clinic", "patient", "medicare"]
    summary: str
    verified_facts: list[str] = Field(default_factory=list)
    outcome: str = "unknown"


class CallPlan(BaseModel):
    action: PlannerAction = "none"
    target_id: str | None = None
    target_name: str | None = None
    goal: str = ""
    facts_to_share: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    reason: str = ""


class CaseState(BaseModel):
    case_id: str
    goal: str
    status: CaseStatus = "intake"
    human_needed: bool = False
    escalation: Escalation | None = None
    patient: Patient
    equipment: Equipment
    pcp: PCPState
    suppliers: list[SupplierRecord] = Field(default_factory=list)
    selected_supplier_id: str | None = None
    delivery: Delivery = Field(default_factory=Delivery)
    patient_updates: list[PatientUpdate] = Field(default_factory=list)
    event_log: list[EventLogEntry] = Field(default_factory=list)
    known_facts: dict[str, Any] = Field(default_factory=dict)
    coordinator_context: list[str] = Field(default_factory=list)
    call_memory: list[CallMemoryRecord] = Field(default_factory=list)
    next_call_plan: CallPlan | None = None

    def log(self, actor: str, action: str, detail: str) -> None:
        self.event_log.append(EventLogEntry(actor=actor, action=action, detail=detail))

    def get_supplier(self, supplier_id: str) -> SupplierRecord | None:
        for s in self.suppliers:
            if s.id == supplier_id:
                return s
        return None


class SupplierCallResult(BaseModel):
    worker_type: Literal["supplier_voice"] = "supplier_voice"
    supplier_id: str
    outcome: Literal[
        "viable",
        "rejected",
        "callback",
        "no_answer",
        "voicemail",
        "unclear",
    ]
    fields: dict[str, Any] = Field(default_factory=dict)
    referral_leads: list[ReferralLead] = Field(default_factory=list)
    callback_at: str | None = None
    summary: str
    confidence: float = 0.5
    needs_human: bool = False
    transcript: list[dict[str, str]] = Field(default_factory=list)


class PCPCallResult(BaseModel):
    worker_type: Literal["pcp_voice"] = "pcp_voice"
    outcome: Literal[
        "order_submitted",
        "in_queue",
        "never_got_request",
        "wrong_order",
        "no_answer",
        "callback",
        "received",
    ]
    order_status: OrderStatus
    order: WrittenOrder | None = None
    callback_at: str | None = None
    summary: str
    confidence: float = 0.5
    needs_human: bool = False
    transcript: list[dict[str, str]] = Field(default_factory=list)
