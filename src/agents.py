from __future__ import annotations

import json
import re
from typing import Any

from src.bus import Envelope, LocalBus
from src.llm import chat_json, chat_text
from src.models import CaseState, SupplierRecord


class BaseAgent:
    agent_id: str
    role: str

    def __init__(self, bus: LocalBus, *, simulate: bool = False):
        self.bus = bus
        self.simulate = simulate
        bus.register(self.agent_id, self.handle)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        raise NotImplementedError

    async def _talk(
        self,
        *,
        self_system: str,
        other_system: str,
        opener: str,
        max_turns: int = 6,
        counterpart_id: str = "navigator",
    ) -> list[dict[str, str]]:
        """Local agent↔agent dialogue. counterpart_id is who the manager-side speaker is."""
        shape_callee = (
            "PHONE CALL RULES: First line is a greeting only "
            "(your organization name + how can I help). "
            "Do not answer the request, quote inventory, or say [END] on that greeting. "
            "Wait for the caller. Answer only what they asked. "
            "When the call is done, one-sentence goodbye then [END]."
        )
        shape_caller = (
            "PHONE CALL RULES: Wait for their greeting. Then identify yourself. "
            "Ask what you need. Do not [END] on your first speaking turn. "
            "When you have the answer (or they cannot help), thank them, say goodbye, [END]."
        )
        transcript: list[dict[str, str]] = []
        msgs_self = [
            {"role": "system", "content": self_system + "\n" + shape_callee},
            {"role": "user", "content": opener},
        ]
        msgs_other = [
            {"role": "system", "content": other_system + "\n" + shape_caller},
        ]

        self_line = await chat_text(msgs_self, temperature=0.45)
        self_line = self._strip_early_end(self_line, turn_index=0)
        transcript.append({"speaker": self.agent_id, "text": self_line})
        msgs_self.append({"role": "assistant", "content": self_line})
        msgs_other.append({"role": "user", "content": self_line})

        for turn_i in range(max_turns - 1):
            other_line = await chat_text(msgs_other, temperature=0.5)
            other_line = self._strip_early_end(other_line, turn_index=turn_i)
            transcript.append({"speaker": counterpart_id, "text": other_line})
            msgs_other.append({"role": "assistant", "content": other_line})
            msgs_self.append({"role": "user", "content": other_line})
            if self._is_end(other_line):
                if not self._is_end(self_line):
                    close = await chat_text(
                        msgs_self
                        + [
                            {
                                "role": "user",
                                "content": "The caller is wrapping up. One-sentence goodbye, then [END].",
                            }
                        ],
                        temperature=0.2,
                    )
                    transcript.append({"speaker": self.agent_id, "text": close})
                break
            self_line = await chat_text(msgs_self, temperature=0.4)
            transcript.append({"speaker": self.agent_id, "text": self_line})
            msgs_self.append({"role": "assistant", "content": self_line})
            msgs_other.append({"role": "user", "content": self_line})
            if self._is_end(self_line):
                if not any(self._is_end(t["text"]) and t["speaker"] == counterpart_id for t in transcript):
                    close = await chat_text(
                        msgs_other
                        + [
                            {
                                "role": "user",
                                "content": "They are saying goodbye. One-sentence goodbye, then [END].",
                            }
                        ],
                        temperature=0.2,
                    )
                    transcript.append({"speaker": counterpart_id, "text": close})
                break
        return transcript

    @staticmethod
    def _is_end(text: str) -> bool:
        t = text or ""
        return "[END]" in t or "[NO_ANSWER]" in t or "[END_CALL]" in t

    @staticmethod
    def _strip_early_end(text: str, *, turn_index: int) -> str:
        if turn_index != 0:
            return text
        return re.sub(r"\[(?:END|END_CALL)\]", "", text or "").strip()


class NavigatorAgent(BaseAgent):
    """Care advocate / manager — initiates outreach to all other local parties."""

    agent_id = "navigator"
    role = "care_navigator"

    def __init__(self, bus: LocalBus, case: CaseState, *, simulate: bool = False):
        self.case = case
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        return {"ok": True, "note": "navigator is initiator; inbound ignored"}


class PatientAgent(BaseAgent):
    agent_id = "eleanor"
    role = "patient"

    def __init__(self, bus: LocalBus, case: CaseState, *, simulate: bool = False):
        self.case = case
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        kind = envelope.kind
        body = envelope.body
        if kind == "update":
            msg = body.get("message", "")
            if self.simulate:
                return {
                    "understood": True,
                    "ack": "Thank you for explaining. I understand I may owe about 20%.",
                    "confused": False,
                    "transcript": [
                        {"speaker": "navigator", "text": msg},
                        {
                            "speaker": "eleanor",
                            "text": "Thank you. I understand the 20% coinsurance.",
                        },
                    ],
                }
            try:
                data = await chat_json(
                    [
                        {
                            "role": "system",
                            "content": (
                                f"You are {self.case.patient.name}, 72, on Original Medicare Part B "
                                "with no Medigap. Respond as JSON "
                                '{"understood":true,"ack":"short spoken reply","confused":false}.'
                            ),
                        },
                        {"role": "user", "content": msg},
                    ]
                )
                ack = data.get("ack") or "Okay, thank you."
                return {
                    "understood": bool(data.get("understood", True)),
                    "ack": ack,
                    "confused": bool(data.get("confused", False)),
                    "transcript": [
                        {"speaker": "navigator", "text": msg},
                        {"speaker": "eleanor", "text": ack},
                    ],
                }
            except Exception:  # noqa: BLE001
                ack = "Thank you for explaining. I understand I may owe about 20%."
                return {
                    "understood": True,
                    "ack": ack,
                    "confused": False,
                    "transcript": [
                        {"speaker": "navigator", "text": msg},
                        {"speaker": "eleanor", "text": ack},
                    ],
                }
        return {"error": f"unknown kind {kind}"}


class ClinicAgent(BaseAgent):
    agent_id = "clinic"
    role = "pcp_reception"

    def __init__(self, bus: LocalBus, case: CaseState, *, simulate: bool = False):
        self.case = case
        self.touches = 0
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        if envelope.kind != "chase_written_order":
            return {"error": "unsupported"}

        if self.simulate:
            return self._scripted()

        # LLM flavor for the conversation; outcome still follows two-touch ground truth
        gt = self.case.pcp.ground_truth
        if self.touches == 0:
            touch_rule = (
                "FIRST CALL: the written order is NOT signed. Put it in Dr. Chen's "
                "signature queue. Do not say it is ready."
            )
        else:
            touch_rule = (
                "FOLLOW-UP CALL: Dr. Chen HAS signed the K0001 written order. "
                "Tell the navigator it is ready to collect. Do not repeat the first-call story."
            )
        transcript = await self._talk(
            self_system=(
                f"You are front desk at {self.case.pcp.clinic} for {self.case.pcp.name}. "
                f"Ground truth: {json.dumps(gt)}. Touches so far: {self.touches}. "
                f"{touch_rule} "
                "This call is ONLY with the CARE NAVIGATOR. "
                "Your job is the written order: unsigned vs in Dr. Chen's signature queue vs signed. "
                "FORBIDDEN: contacting DME suppliers, waiting on a supplier reply, tracking "
                "supplier status, ship dates, or carriers. You do NOT route orders to shops. "
                "When signed, you give the order to the NAVIGATOR; they take it to the supplier. "
                "If asked about a supplier say: we don't work with the supplier; the navigator does. "
                "Short replies. Include [END] when done."
            ),
            other_system=(
                f"You are the CARE NAVIGATOR (not the patient). You are the only party "
                f"allowed to contact this clinic. Chase the written DME order for patient "
                f"{self.case.patient.name}, DOB {self.case.patient.dob}, "
                f"equipment {self.case.equipment.hcpcs}. "
                "ONLY ask: is a signed written K0001 order ready for you to collect? "
                "FORBIDDEN: asking the clinic about supplier status, supplier replies, "
                "ship dates, carriers, or whether they routed/sent anything to a DME shop. "
                "The clinic does not talk to suppliers. You already call shops yourself. "
                "When the order is signed, say you will take it to the supplier. Then [END]."
            ),
            opener=(
                f"The phone rang at {self.case.pcp.clinic}. Pick up. "
                "Greeting only: clinic name and how can I help. Do not mention the order yet."
            ),
            max_turns=4,
            counterpart_id="navigator",
        )
        result = self._scripted()
        result["transcript"] = transcript
        return result

    def _scripted(self) -> dict[str, Any]:
        if self.touches == 0:
            self.touches += 1
            return {
                "outcome": "in_queue",
                "order_status": "in_queue",
                "order": None,
                "summary": "Written order was never routed; now in nurse signature queue.",
                "transcript": [
                    {
                        "speaker": "clinic",
                        "text": f"{self.case.pcp.clinic}, front desk. How can I help you?",
                    },
                    {
                        "speaker": "navigator",
                        "text": f"This is the care navigator for {self.case.patient.name}. Checking on a written order for K0001.",
                    },
                    {
                        "speaker": "clinic",
                        "text": "It never reached the nurse — I'll put it in Dr. Chen's signature queue today. When it's signed, we give it to you. We don't send orders to DME suppliers.",
                    },
                    {
                        "speaker": "navigator",
                        "text": "Thank you. I'll call back for the signed copy. Goodbye. [END]",
                    },
                    {
                        "speaker": "clinic",
                        "text": "You're welcome. Goodbye. [END]",
                    },
                ],
            }
        self.touches += 1
        return {
            "outcome": "received",
            "order_status": "received",
            "order": {
                "signed": True,
                "signed_at": "2026-08-10",
                "equipment_text": "standard manual wheelchair",
                "hcpcs": self.case.equipment.hcpcs,
                "matches_request": True,
                "source": "sunrise_family_medicine",
            },
            "summary": "Signed written order for K0001 ready for the navigator to take to the supplier.",
            "transcript": [
                {
                    "speaker": "clinic",
                    "text": f"{self.case.pcp.clinic}, front desk. How can I help you?",
                },
                {
                    "speaker": "navigator",
                    "text": f"Following up — is the written order signed for {self.case.patient.name}?",
                },
                {
                    "speaker": "clinic",
                    "text": "Yes. Standard manual wheelchair K0001 is signed and ready for you to take to the supplier. We are not waiting on any shop.",
                },
                {
                    "speaker": "navigator",
                    "text": "Perfect. I'll collect it and take it to the supplier. Thank you. Goodbye. [END]",
                },
                {
                    "speaker": "clinic",
                    "text": "You're welcome. Goodbye. [END]",
                },
            ],
        }


class SupplierAgent(BaseAgent):
    role = "dme_supplier"

    def __init__(
        self,
        bus: LocalBus,
        supplier: SupplierRecord,
        case: CaseState,
        *,
        simulate: bool = False,
    ):
        self.agent_id = f"supplier:{supplier.id}"
        self.supplier = supplier
        self.case = case
        self.attempts = 0
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        if envelope.kind != "availability_check":
            return {"error": "unsupported"}
        self.attempts += 1
        if self.simulate:
            return self._scripted()

        gt = self.supplier.ground_truth
        transcript = await self._talk(
            self_system=(
                f"You work the phone at DME supplier {self.supplier.name}. "
                f"Follow ground truth exactly: {json.dumps(gt)}. Short phone-style replies. "
                "The caller is a CARE NAVIGATOR, not the patient. "
                "Do NOT offer to call the patient's doctor, clinic, or PCP. "
                "You do not chase written orders. If they ask about the doctor, say: "
                "the navigator handles the PCP; you only need the faxed order later. "
                "You MAY refer another supplier name/phone if you cannot help. "
                "If behavior is no_answer, voicemail once and [NO_ANSWER]."
            ),
            other_system=(
                "You are the CARE NAVIGATOR coordinating DME. You are NOT the patient. "
                f"Never say 'I am {self.case.patient.name}'. Speak as the advocate calling "
                f"on behalf of {self.case.patient.name} in {self.case.patient.city}, "
                f"Original Medicare Part B, need {self.case.equipment.description} "
                f"({self.case.equipment.hcpcs}).\n"
                "ONLY learn: taking new Medicare? K0001 in stock? deliver to Chicago? ETA "
                "once a written order is faxed?\n"
                "If they cannot help, ask for another SUPPLIER referral (name + phone).\n"
                "FORBIDDEN: asking the supplier to contact Dr. Chen, the clinic, or any PCP. "
                "You (the navigator) will contact the doctor separately. Do not discuss "
                "having the shop call the physician.\n"
                "Keep it to a few short turns. Then thank them and [END]."
            ),
            opener=(
                f"The phone rang at {self.supplier.name}. Pick up. "
                "Greeting only: say the shop name and ask how you can help. "
                "Do not mention wheelchairs, Medicare, stock, or ETAs until the caller asks."
            ),
            max_turns=5,
            counterpart_id="navigator",
        )
        # Prefer ground-truth outcomes for known behaviors so world stays testable.
        # Optional LLM extract is best-effort (local models often emit invalid JSON).
        scripted = self._scripted()
        scripted["transcript"] = transcript
        try:
            extracted = await chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Extract JSON: outcome viable|rejected|callback|no_answer|unclear; "
                            "fields{taking_new_medicare,has_k0001,serves_area,delivery_eta_days,responsive}; "
                            "referral_leads[{name,phone,note}]; summary; confidence."
                        ),
                    },
                    {"role": "user", "content": json.dumps(transcript)},
                ]
            )
            scripted["llm_extract"] = extracted
        except Exception as exc:  # noqa: BLE001
            scripted["llm_extract_error"] = str(exc)
        return scripted

    def _scripted(self) -> dict[str, Any]:
        gt = self.supplier.ground_truth or {}
        behavior = gt.get("behavior")
        sid = self.supplier.id
        nav_ask = (
            f"Hi, this is the care navigator for {self.case.patient.name} in Chicago. "
            "Do you take new Medicare patients, and do you have a K0001 standard manual wheelchair?"
        )

        def call(supplier_line: str, nav: str = nav_ask) -> list[dict[str, str]]:
            return [
                {
                    "speaker": self.agent_id,
                    "text": f"{self.supplier.name}, how can I help you?",
                },
                {"speaker": "navigator", "text": nav},
                {"speaker": self.agent_id, "text": supplier_line},
                {
                    "speaker": "navigator",
                    "text": "Thanks for your time. Goodbye. [END]",
                },
                {
                    "speaker": self.agent_id,
                    "text": "You're welcome. Goodbye. [END]",
                },
            ]

        if behavior == "no_answer":
            return {
                "supplier_id": sid,
                "outcome": "voicemail",
                "fields": {"responsive": False},
                "referral_leads": [],
                "summary": "No live answer",
                "transcript": [
                    {
                        "speaker": self.agent_id,
                        "text": f"You've reached {self.supplier.name}. Please leave a message. [NO_ANSWER]",
                    },
                    {
                        "speaker": "navigator",
                        "text": "I'll try again later. Goodbye. [END]",
                    },
                ],
            }

        if behavior == "decline_not_taking_new":
            ref = gt.get("referral")
            leads = []
            said = "We're not taking new Medicare patients right now."
            if ref:
                said += f" Try {ref['name']} at {ref['phone']}."
                leads.append({**ref, "source_supplier_id": sid})
            return {
                "supplier_id": sid,
                "outcome": "rejected",
                "fields": {
                    "taking_new_medicare": False,
                    "has_k0001": gt.get("has_k0001"),
                    "serves_area": gt.get("serves_area"),
                },
                "referral_leads": leads,
                "summary": "Not taking new Medicare patients",
                "transcript": call(said),
            }

        if behavior == "no_stock":
            ref = gt.get("referral")
            leads = []
            said = "We take Medicare but we're out of standard manual K0001."
            if ref:
                said += f" You could try {ref['name']} ({ref['phone']})."
                leads.append({**ref, "source_supplier_id": sid})
            return {
                "supplier_id": sid,
                "outcome": "rejected",
                "fields": {
                    "taking_new_medicare": True,
                    "has_k0001": False,
                    "serves_area": True,
                },
                "referral_leads": leads,
                "summary": "Out of stock on K0001",
                "transcript": call(said),
            }

        if behavior == "outside_service_area":
            ref = gt.get("referral")
            leads = []
            said = "We have manuals and take Medicare, but we don't deliver to that address."
            if ref:
                said += f" Try {ref['name']} at {ref['phone']}."
                leads.append({**ref, "source_supplier_id": sid})
            return {
                "supplier_id": sid,
                "outcome": "rejected",
                "fields": {
                    "taking_new_medicare": True,
                    "has_k0001": True,
                    "serves_area": False,
                },
                "referral_leads": leads,
                "summary": "Outside service area",
                "transcript": call(said),
            }

        if behavior == "says_yes_then_evasive":
            return {
                "supplier_id": sid,
                "outcome": "unclear",
                "fields": {
                    "taking_new_medicare": True,
                    "has_k0001": True,
                    "serves_area": True,
                    "delivery_eta_days": None,
                },
                "referral_leads": [],
                "summary": "Sounded positive but would not commit — unreliable",
                "transcript": call(
                    "We can probably help… warehouse is checking, someone will call you back."
                ),
            }

        # viable
        eta = gt.get("delivery_eta_days") or 5
        return {
            "supplier_id": sid,
            "outcome": "viable",
            "fields": {
                "taking_new_medicare": True,
                "has_k0001": True,
                "serves_area": True,
                "delivery_eta_days": eta,
                "responsive": True,
            },
            "referral_leads": [],
            "summary": f"Viable — ETA ~{eta} days after written order",
            "transcript": call(
                f"Yes — Original Medicare, K0001 in stock, we deliver in Chicago, "
                f"about {eta} business days after we receive the written order."
            ),
        }


class MedicareAgent(BaseAgent):
    """Local Medicare rules oracle — validates claim readiness (not a phone call)."""

    agent_id = "medicare"
    role = "medicare_part_b"

    def __init__(self, bus: LocalBus, case: CaseState, *, simulate: bool = False):
        self.case = case
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        if envelope.kind != "coverage_check":
            return {"error": "unsupported"}
        body = envelope.body
        has_order = bool(body.get("written_order"))
        order_ok = bool(body.get("order_matches_k0001", False))
        supplier_enrolled = bool(body.get("supplier_medicare_enrolled", False))
        supplier_viable = bool(body.get("supplier_can_fulfill", False))
        code = body.get("hcpcs")

        blockers = []
        if not has_order:
            blockers.append("missing_written_order")
        if has_order and not order_ok:
            blockers.append("order_code_mismatch")
        if not supplier_enrolled:
            blockers.append("supplier_not_enrolled")
        if not supplier_viable:
            blockers.append("supplier_cannot_fulfill")
        if code != "K0001":
            blockers.append("unexpected_hcpcs")

        payable = len(blockers) == 0
        return {
            "payable": payable,
            "patient_responsibility": (
                "About 20% coinsurance of the Medicare-approved amount after Part B deductible "
                "(no Medigap on file)."
                if payable
                else None
            ),
            "blockers": blockers,
            "summary": (
                "Claim would be positioned to pay under Part B DME rules."
                if payable
                else f"Not ready: {', '.join(blockers)}"
            ),
            "transcript": [
                {
                    "speaker": "navigator",
                    "text": f"Coverage check for {self.case.patient.name}, {code}, order={has_order}, supplier_ok={supplier_viable}",
                },
                {
                    "speaker": "medicare",
                    "text": (
                        "Part B can cover standard manual wheelchair when written order + enrolled "
                        f"supplier align. Result: {'PAYABLE path' if payable else 'BLOCKED'} — "
                        + (", ".join(blockers) if blockers else "no blockers")
                        + ". Patient typically owes ~20% coinsurance without Medigap."
                    ),
                },
            ],
        }
