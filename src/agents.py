from __future__ import annotations

import re
from typing import Any

from src.bus import Envelope, LocalBus
from src.coordinator import (
    GOODBYE_FROM_CALLEE,
    GOODBYE_FROM_CALLER,
    analyze_call,
    append_coordinator_context,
    compose_patient_reply,
    conclusion_from_clinic_call,
    conclusion_from_supplier_call,
    json_turn,
    live_context_json,
)
from src.models import CaseState, SupplierRecord

from prompts.loader import (
    build_system_prompt,
    clinic_opener,
    clinic_touch_extra,
    supplier_opener,
)


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
        transcript: list[dict[str, str]] = []
        msgs_self = [
            {"role": "system", "content": self_system},
            {"role": "user", "content": opener},
        ]
        msgs_other = [
            {"role": "system", "content": other_system},
        ]

        self_line = await json_turn(msgs_self, temperature=0.45)
        self_line = self._strip_early_end(self_line, turn_index=0)
        transcript.append({"speaker": self.agent_id, "text": self_line})
        msgs_self.append({"role": "assistant", "content": self_line})
        msgs_other.append({"role": "user", "content": self_line})

        for turn_i in range(max_turns - 1):
            other_line = await json_turn(msgs_other, temperature=0.5)
            other_line = self._strip_early_end(other_line, turn_index=turn_i)
            transcript.append({"speaker": counterpart_id, "text": other_line})
            msgs_other.append({"role": "assistant", "content": other_line})
            msgs_self.append({"role": "user", "content": other_line})
            if self._is_end(other_line):
                if not self._is_end(self_line):
                    close = await json_turn(
                        msgs_self
                        + [
                            {
                                "role": "user",
                                "content": GOODBYE_FROM_CALLER,
                            }
                        ],
                        temperature=0.2,
                    )
                    transcript.append({"speaker": self.agent_id, "text": close})
                break
            self_line = await json_turn(msgs_self, temperature=0.4)
            transcript.append({"speaker": self.agent_id, "text": self_line})
            msgs_self.append({"role": "assistant", "content": self_line})
            msgs_other.append({"role": "user", "content": self_line})
            if self._is_end(self_line):
                if not any(self._is_end(t["text"]) and t["speaker"] == counterpart_id for t in transcript):
                    close = await json_turn(
                        msgs_other
                        + [
                            {
                                "role": "user",
                                "content": GOODBYE_FROM_CALLEE,
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

    def __init__(
        self,
        bus: LocalBus,
        case: CaseState,
        *,
        simulate: bool = False,
        live_server: Any = None,
    ):
        self.case = case
        self.live_server = live_server
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        kind = envelope.kind
        body = envelope.body
        if kind == "update":
            msg = body.get("message", "")
            if self.live_server:
                from src.live_voice import CallSpec

                context = live_context_json(
                    self.case,
                    target_id="patient",
                    target_name=self.case.patient.name,
                    call_type="patient",
                    goal="Explain the current case status and confirm understanding.",
                    facts_to_share=[msg],
                    questions=["Ask whether the patient understands or has questions."],
                )
                result = await self.live_server.run_call(
                    CallSpec(
                        role="eleanor",
                        title="Call for Eleanor Martinez",
                        human_hint=(
                            "You are Eleanor Martinez. The care coordinator is calling "
                            "about your wheelchair. Answer in your own voice."
                        ),
                        navigator_system=build_system_prompt(
                            "navigator",
                            side="caller",
                            spoken=True,
                            extra=f"INPUT_CONTEXT_JSON:\n{context}",
                        )
                        + "\nOwn unresolved requests from the patient. If an answer is not "
                        "supported by context, say that you cannot confirm it yet and that "
                        "you will follow up. Do not invent facts or delegate the follow-up "
                        "unless context explicitly says another party has accepted it.",
                        navigator_speaks_first=True,
                        first_navigator_line=msg,
                        max_turns=6,
                    )
                )
                transcript = result.transcript or [
                    {"speaker": "navigator", "text": msg},
                ]
                ack = next(
                    (t["text"] for t in reversed(transcript) if t.get("speaker") == "eleanor"),
                    "Okay, thank you.",
                )
                analysis: dict[str, Any] | None = None
                try:
                    analysis = await analyze_call(
                        self.case,
                        call_type="patient",
                        transcript=transcript,
                        counterpart_name=self.case.patient.name,
                        status_callback=(
                            self.live_server.notify_analysis_status
                            if self.live_server
                            else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    append_coordinator_context(
                        self.case,
                        f"Spoke with {self.case.patient.name} at {self.case.patient.phone} | "
                        f"Live call ended ({result.ended_reason}) | Note: analyze failed ({exc})",
                    )
                follow_up = bool(
                    analysis
                    and (analysis.get("next_call") or {}).get("action")
                    in {"call_supplier", "call_clinic", "call_medicare"}
                )
                return {
                    "understood": not follow_up,
                    "ack": ack,
                    "confused": follow_up,
                    "transcript": transcript,
                    "live": True,
                    "ended_reason": result.ended_reason,
                }
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
                ack = await json_turn(
                    [
                        {
                            "role": "system",
                            "content": build_system_prompt("patient"),
                        },
                        {"role": "user", "content": msg},
                    ]
                )
            except Exception:  # noqa: BLE001
                ack = "Thank you for explaining. I understand I may owe about 20%."
            transcript = [
                {"speaker": "navigator", "text": msg},
                {"speaker": "eleanor", "text": ack},
            ]
            try:
                await analyze_call(
                    self.case,
                    call_type="patient",
                    transcript=transcript,
                    counterpart_name=self.case.patient.name,
                    status_callback=(
                        self.live_server.notify_analysis_status
                        if self.live_server
                        else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                append_coordinator_context(
                    self.case,
                    f"Spoke with {self.case.patient.name} at {self.case.patient.phone} | "
                    f"Patient heard: {ack} | Note: analyze failed ({exc})",
                )
            return {
                "understood": True,
                "ack": ack,
                "confused": False,
                "transcript": transcript,
            }
        return {"error": f"unknown kind {kind}"}


class ClinicAgent(BaseAgent):
    agent_id = "clinic"
    role = "pcp_reception"

    def __init__(
        self,
        bus: LocalBus,
        case: CaseState,
        *,
        simulate: bool = False,
        live_server: Any = None,
    ):
        self.case = case
        self.touches = 0
        self.live_server = live_server
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        if envelope.kind != "chase_written_order":
            return {"error": "unsupported"}

        if self.live_server:
            from src.live_voice import CallSpec

            context = live_context_json(
                self.case,
                target_id="clinic",
                target_name=self.case.pcp.clinic,
                call_type="clinic",
                goal="Confirm whether the signed K0001 written order is ready.",
                facts_to_share=[
                    f"Patient: {self.case.patient.name}",
                    f"Date of birth: {self.case.patient.dob}",
                    f"Requested equipment: {self.case.equipment.hcpcs}",
                ],
                questions=[
                    "Is the signed written order ready?",
                    "If not, what is its status and when should we follow up?",
                ],
            )
            live = await self.live_server.run_call(
                CallSpec(
                    role="clinic",
                    title="Sunrise Family Medicine",
                    human_hint=(
                        f"You are front desk at Sunrise Family Medicine for Dr. Sarah Chen. "
                        f"{clinic_touch_extra(touches=self.touches)} "
                        "Pick up, greet with the clinic name, then answer whatever they ask."
                    ),
                    navigator_system=build_system_prompt(
                        "navigator",
                        side="caller",
                        mode="clinic",
                        spoken=True,
                        extra=f"INPUT_CONTEXT_JSON:\n{context}",
                    ),
                    opener=clinic_opener(),
                    navigator_speaks_first=False,
                    max_turns=8,
                )
            )
            transcript = live.transcript
            if not transcript:
                result = self._scripted()
                append_coordinator_context(
                    self.case,
                    (
                        f"Live clinic call ended ({live.ended_reason}) before any "
                        "conversation was captured."
                    ),
                )
            else:
                try:
                    analysis = await analyze_call(
                        self.case,
                        call_type="clinic",
                        transcript=transcript,
                        counterpart_name=self.case.pcp.clinic,
                        status_callback=(
                            self.live_server.notify_analysis_status
                            if self.live_server
                            else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    result = self._scripted()
                    append_coordinator_context(
                        self.case,
                        conclusion_from_clinic_call(self.case, result)
                        + f" | Note: live analyze failed ({exc})",
                    )
                else:
                    patch = analysis.get("state_patch") or {}
                    status = patch.get("order_status")
                    if status:
                        self.touches += 1
                        result = {
                            "outcome": analysis.get("outcome") or status,
                            "order_status": status,
                            "order": patch.get("order"),
                            "summary": analysis.get("summary") or "Clinic call completed.",
                        }
                    else:
                        result = self._scripted()
            result["transcript"] = transcript
            result["live"] = True
            result["ended_reason"] = live.ended_reason
            return result

        if self.simulate:
            result = self._scripted()
            append_coordinator_context(
                self.case,
                conclusion_from_clinic_call(self.case, result),
            )
            return result

        transcript = await self._talk(
            self_system=build_system_prompt(
                "clinic",
                side="callee",
                extra=f"Touches so far: {self.touches}.\n{clinic_touch_extra(touches=self.touches)}",
            ),
            other_system=build_system_prompt(
                "navigator",
                side="caller",
                mode="clinic",
            ),
            opener=clinic_opener(),
            max_turns=4,
            counterpart_id="navigator",
        )
        try:
            await analyze_call(
                self.case,
                call_type="clinic",
                transcript=transcript,
                counterpart_name=self.case.pcp.clinic,
                status_callback=(
                    self.live_server.notify_analysis_status
                    if self.live_server
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            result = self._scripted()
            append_coordinator_context(
                self.case,
                conclusion_from_clinic_call(self.case, result)
                + f" | Note: LLM analyze failed ({exc})",
            )
        else:
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
        live_server: Any = None,
    ):
        self.agent_id = f"supplier:{supplier.id}"
        self.supplier = supplier
        self.case = case
        self.attempts = 0
        self.live_server = live_server
        super().__init__(bus, simulate=simulate)

    async def handle(self, envelope: Envelope) -> dict[str, Any]:
        if envelope.kind != "availability_check":
            return {"error": "unsupported"}
        self.attempts += 1
        if self.simulate:
            result = self._scripted()
            append_coordinator_context(
                self.case,
                conclusion_from_supplier_call(self.case, self.supplier, result),
            )
            return result

        if self.live_server:
            from src.live_voice import CallSpec

            context = live_context_json(
                self.case,
                target_id=self.supplier.id,
                target_name=self.supplier.name,
                call_type="supplier",
                goal="Determine whether this supplier can fulfill the K0001 request.",
                facts_to_share=[
                    f"Coverage: {self.case.patient.plan}",
                    f"Equipment: {self.case.equipment.hcpcs} {self.case.equipment.description}",
                    f"Delivery city: {self.case.patient.city}",
                ],
                questions=[
                    "Are you accepting new Original Medicare patients?",
                    "Is K0001 in stock?",
                    f"Do you deliver to {self.case.patient.city}?",
                    "What is the ETA after receiving the written order?",
                ],
            )
            live = await self.live_server.run_call(
                CallSpec(
                    role=self.agent_id,
                    title=self.supplier.name,
                    human_hint=(
                        f"You are answering for {self.supplier.name} at "
                        f"{self.supplier.phone}. Act as the DME supplier representative. "
                        "Greet with the business name, then answer the navigator."
                    ),
                    navigator_system=build_system_prompt(
                        "navigator",
                        side="caller",
                        mode="supplier",
                        spoken=True,
                        extra=f"INPUT_CONTEXT_JSON:\n{context}",
                    ),
                    opener=supplier_opener(supplier_name=self.supplier.name),
                    navigator_speaks_first=False,
                    max_turns=8,
                )
            )
            transcript = live.transcript
            result = self._scripted()
            if transcript:
                try:
                    analysis = await analyze_call(
                        self.case,
                        call_type="supplier",
                        transcript=transcript,
                        counterpart_name=self.supplier.name,
                        supplier=self.supplier,
                        status_callback=(
                            self.live_server.notify_analysis_status
                            if self.live_server
                            else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    append_coordinator_context(
                        self.case,
                        conclusion_from_supplier_call(
                            self.case,
                            self.supplier,
                            result,
                        )
                        + f" | Note: live analyze failed ({exc})",
                    )
                else:
                    patch = analysis.get("state_patch") or {}
                    outcome = patch.get("supplier_outcome") or analysis.get("outcome")
                    if outcome in {
                        "viable",
                        "rejected",
                        "callback",
                        "no_answer",
                        "voicemail",
                        "unclear",
                    }:
                        result = {
                            "supplier_id": self.supplier.id,
                            "outcome": outcome,
                            "fields": patch.get("supplier_fields") or {},
                            "referral_leads": patch.get("referral_leads") or [],
                            "summary": analysis.get("summary") or "Supplier call completed.",
                            "confidence": 0.8,
                        }
            else:
                append_coordinator_context(
                    self.case,
                    (
                        f"Live supplier call to {self.supplier.name} at "
                        f"{self.supplier.phone} ended ({live.ended_reason}) before "
                        "any conversation was captured."
                    ),
                )
            result["transcript"] = transcript
            result["live"] = True
            result["ended_reason"] = live.ended_reason
            result.setdefault("supplier_id", self.supplier.id)
            return result

        transcript = await self._talk(
            self_system=build_system_prompt(
                self.supplier.id,
                side="callee",
            ),
            other_system=build_system_prompt(
                "navigator",
                side="caller",
                mode="supplier",
            ),
            opener=supplier_opener(supplier_name=self.supplier.name),
            max_turns=5,
            counterpart_id="navigator",
        )
        try:
            await analyze_call(
                self.case,
                call_type="supplier",
                transcript=transcript,
                counterpart_name=self.supplier.name,
                supplier=self.supplier,
                status_callback=(
                    self.live_server.notify_analysis_status
                    if self.live_server
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            result = self._scripted()
            append_coordinator_context(
                self.case,
                conclusion_from_supplier_call(self.case, self.supplier, result)
                + f" | Note: LLM analyze failed ({exc})",
            )
        else:
            result = self._scripted()
        result["transcript"] = transcript
        result.setdefault("supplier_id", self.supplier.id)
        return result

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
    """Medicare Part B rules oracle, optionally represented by a live human."""

    agent_id = "medicare"
    role = "medicare_part_b"

    def __init__(
        self,
        bus: LocalBus,
        case: CaseState,
        *,
        simulate: bool = False,
        live_server: Any = None,
    ):
        self.case = case
        self.live_server = live_server
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
        result = {
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
        if self.live_server:
            from src.live_voice import CallSpec

            context = live_context_json(
                self.case,
                target_id="medicare",
                target_name="Medicare Part B",
                call_type="medicare",
                goal="Verify coverage readiness and patient responsibility.",
                facts_to_share=[
                    f"HCPCS: {code}",
                    f"Matching written order: {has_order and order_ok}",
                    f"Supplier enrolled and viable: {supplier_enrolled and supplier_viable}",
                ],
                questions=[
                    "Is the coverage path ready?",
                    "What patient coinsurance should be explained?",
                ],
            )
            question = (
                f"I'm calling about Part B coverage readiness for {self.case.patient.name}, "
                f"HCPCS {code}. We have a matching written order: {has_order}. "
                f"The selected supplier is enrolled and able to fulfill: {supplier_viable}. "
                "Can you confirm the coverage path and patient responsibility?"
            )
            live = await self.live_server.run_call(
                CallSpec(
                    role="medicare",
                    title="Medicare Part B",
                    human_hint=(
                        "You are a Medicare Part B representative. Answer the care "
                        "coordinator's coverage-readiness questions."
                    ),
                    navigator_system=(
                        build_system_prompt(
                            "navigator",
                            side="caller",
                            spoken=True,
                            extra=f"INPUT_CONTEXT_JSON:\n{context}",
                        )
                        + "\nYou are calling a Medicare Part B representative to verify "
                        "coverage readiness and patient coinsurance."
                    ),
                    navigator_speaks_first=True,
                    first_navigator_line=question,
                    max_turns=8,
                )
            )
            result["transcript"] = live.transcript
            result["live"] = True
            result["ended_reason"] = live.ended_reason
            if live.transcript:
                try:
                    result["analysis"] = await analyze_call(
                        self.case,
                        call_type="medicare",
                        transcript=live.transcript,
                        counterpart_name="Medicare Part B",
                        status_callback=self.live_server.notify_analysis_status,
                    )
                except Exception as exc:  # noqa: BLE001
                    append_coordinator_context(
                        self.case,
                        f"Spoke with Medicare Part B | Live call ended "
                        f"({live.ended_reason}) | Note: analyze failed ({exc})",
                    )
        return result
