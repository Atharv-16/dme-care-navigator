from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.models import CaseState
from src.ranking import exhausted_suppliers, next_supplier_batch, viable_suppliers

Action = Literal[
    "dispatch_supplier_calls",
    "dispatch_pcp_chase",
    "request_handoff",
    "notify_patient",
    "escalate",
    "complete",
    "stop",
]


@dataclass
class Decision:
    action: Action
    reason: str
    supplier_ids: list[str] | None = None


def decide(case: CaseState, *, max_parallel: int = 2) -> Decision:
    """
    Deterministic policy gate. LLM conversations happen inside workers;
    sequencing / escalation stays rule-based so demos stay auditable.
    """
    if case.human_needed or case.status == "escalated":
        return Decision("stop", "Human escalation already open")

    if case.status == "completed":
        return Decision("stop", "Case already completed")

    order_ready = (
        case.pcp.order_status == "received"
        and case.pcp.order is not None
        and case.pcp.order.matches_request
    )

    viables = viable_suppliers(case)

    # Handoff when both sides ready
    if order_ready and (case.selected_supplier_id or viables):
        if not case.selected_supplier_id and viables:
            # manager will select before handoff
            return Decision(
                "request_handoff",
                "Written order ready and at least one viable supplier",
                supplier_ids=[viables[0].id],
            )
        if case.delivery.status == "scheduled" and case.patient.coinsurance_explained:
            return Decision("complete", "Delivery scheduled and patient informed")
        if case.delivery.status == "scheduled" and not case.patient.coinsurance_explained:
            return Decision("notify_patient", "Explain status + ~20% coinsurance")
        return Decision(
            "request_handoff",
            "Match order to supplier and schedule delivery",
            supplier_ids=[case.selected_supplier_id] if case.selected_supplier_id else None,
        )

    # Need suppliers
    need_supplier = not viables and case.selected_supplier_id is None
    if need_supplier:
        batch = next_supplier_batch(case, limit=max_parallel)
        if batch:
            # Prefer overlapping PCP chase once we've started supplier work
            if case.pcp.order_status in {"verbal_only", "requested", "in_queue"} and case.pcp.chase_attempts == 0:
                # First do a parallel wave: suppliers + we'll chase PCP next step;
                # kick suppliers first so demo shows parallel supplier calls clearly.
                return Decision(
                    "dispatch_supplier_calls",
                    "No viable supplier yet; calling next ranked batch",
                    supplier_ids=[s.id for s in batch],
                )
            return Decision(
                "dispatch_supplier_calls",
                "Still hunting for a viable supplier",
                supplier_ids=[s.id for s in batch],
            )
        if exhausted_suppliers(case):
            return Decision(
                "escalate",
                "All known suppliers rejected or unresponsive",
            )

    # Chase / re-chase PCP
    if case.pcp.order_status != "received":
        if case.pcp.chase_attempts >= 4:
            return Decision("escalate", "PCP order still not secured after repeated chases")
        return Decision(
            "dispatch_pcp_chase",
            f"Written order status is {case.pcp.order_status}; chasing clinic",
        )

    # Order ready but no supplier somehow
    if order_ready and not viables:
        batch = next_supplier_batch(case, limit=max_parallel)
        if batch:
            return Decision(
                "dispatch_supplier_calls",
                "Order ready but no viable supplier; continue outreach",
                supplier_ids=[s.id for s in batch],
            )
        return Decision("escalate", "Order ready but supplier network exhausted")

    return Decision("escalate", "No actionable path without human judgment")
