from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.agents import (
    ClinicAgent,
    MedicareAgent,
    NavigatorAgent,
    PatientAgent,
    SupplierAgent,
)
from src.coordinator import compose_patient_reply, init_coordinator_context
from prompts.loader import hydrate_case, hydrate_suppliers
from src.bus import Envelope, LocalBus
from src.llm import llm_provider
from src.models import (
    CaseState,
    Escalation,
    PatientUpdate,
    ReferralLead,
    SupplierCallResult,
    SupplierRecord,
    WrittenOrder,
    utc_now,
)
from src.policy import decide
from src.ranking import rank_suppliers, viable_suppliers

console = Console()
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUTPUT = ROOT / "output"


class World:
    """
    Local multi-agent DME simulation.

    Parties on the bus:
      - navigator (care manager)
      - eleanor (patient)
      - clinic (Dr. Chen reception)
      - medicare (Part B rules oracle)
      - supplier:<id> for every directory row
    """

    def __init__(self, case: CaseState, *, simulate: bool = True, voice: bool = False):
        self.case = case
        self.simulate = simulate
        self.voice = voice
        self.bus = LocalBus()
        self.max_parallel = int(os.getenv("MAX_PARALLEL_CALLS", "2"))
        self.max_steps = int(os.getenv("MAX_MANAGER_STEPS", "14"))
        if voice and not simulate:
            rec_audio = OUTPUT / "recordings" / "current" / "audio"
            if rec_audio.exists():
                shutil.rmtree(rec_audio)
            rec_audio.mkdir(parents=True, exist_ok=True)
            os.environ["DME_AUDIO_DIR"] = str(rec_audio)

        self.navigator = NavigatorAgent(self.bus, case, simulate=simulate)
        self.patient = PatientAgent(self.bus, case, simulate=simulate)
        self.clinic = ClinicAgent(self.bus, case, simulate=simulate)
        self.medicare = MedicareAgent(self.bus, case, simulate=simulate)
        self.supplier_agents: dict[str, SupplierAgent] = {}
        for s in case.suppliers:
            ag = SupplierAgent(self.bus, s, case, simulate=simulate)
            self.supplier_agents[s.id] = ag

    @classmethod
    def load(cls, *, simulate: bool = True, voice: bool = False) -> World:
        case_data = hydrate_case(json.loads((DATA / "case_eleanor.json").read_text()))
        suppliers_data = hydrate_suppliers(json.loads((DATA / "suppliers.json").read_text()))
        case = CaseState.model_validate(case_data)
        case.suppliers = [SupplierRecord.model_validate(s) for s in suppliers_data]
        case.status = "in_progress"
        init_coordinator_context(case)
        case.log("world", "boot", f"Loaded case with {len(case.suppliers)} local supplier agents")
        return cls(case, simulate=simulate, voice=voice)

    def _emit(self, msg: str) -> None:
        console.print(f"[bold cyan]NAVIGATOR[/bold cyan] {msg}")

    def _print_transcript(self, title: str, transcript: list[dict[str, str]]) -> None:
        table = Table(title=title, show_lines=False)
        table.add_column("Who", style="bold", width=22)
        table.add_column("Said")
        for turn in transcript:
            table.add_row(str(turn.get("speaker", "?")), turn.get("text", ""))
        console.print(table)

    async def _maybe_speak(self, title: str, transcript: list[dict[str, str]]) -> None:
        if not self.voice or not transcript:
            return
        from src.voice import speak_transcript

        await speak_transcript(
            transcript,
            conversation_name=title,
            play=self.simulate,
        )

    def _print_board(self) -> None:
        table = Table(title="Case board")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("status", self.case.status)
        table.add_row("PCP order", self.case.pcp.order_status)
        table.add_row(
            "viable",
            ", ".join(s.name for s in viable_suppliers(self.case)) or "—",
        )
        table.add_row("selected", self.case.selected_supplier_id or "—")
        table.add_row("delivery", self.case.delivery.status)
        table.add_row("agents", str(len(self.bus.agents())))
        console.print(table)

    def add_supplier_from_lead(self, lead: ReferralLead) -> None:
        digits = re.sub(r"\D", "", lead.phone)
        for existing in self.case.suppliers:
            same_phone = digits and re.sub(r"\D", "", existing.phone) == digits
            same_name = existing.name.lower() == lead.name.lower()
            if same_phone or same_name:
                existing.from_referral = True
                if lead.note:
                    existing.notes.append(lead.note)
                if existing.id not in self.supplier_agents:
                    self.supplier_agents[existing.id] = SupplierAgent(
                        self.bus, existing, self.case, simulate=self.simulate
                    )
                self._emit(f"Referral already in directory → call {existing.name} next")
                return

        new_id = f"lead_{len(self.case.suppliers) + 1:03d}"
        record = SupplierRecord(
            id=new_id,
            name=lead.name,
            phone=lead.phone,
            address="Chicago, IL (referral)",
            from_referral=True,
            notes=[lead.note or "Referral lead"],
            ground_truth={
                "taking_new_medicare": True,
                "has_k0001": True,
                "serves_area": True,
                "delivery_eta_days": 5,
                "behavior": "viable",
                "referral": None,
                "notes_for_actor": "Referred shop — viable Medicare K0001 Chicago.",
            },
        )
        self.case.suppliers.append(record)
        self.supplier_agents[new_id] = SupplierAgent(
            self.bus, record, self.case, simulate=self.simulate
        )
        self.case.log("navigator", "add_lead", f"Registered new supplier agent {record.name}")
        self._emit(f"Spawned supplier agent for lead → {record.name}")

    def merge_supplier_result(self, raw: dict) -> None:
        sid = raw.get("supplier_id")
        supplier = self.case.get_supplier(sid) if sid else None
        if not supplier:
            return
        result = SupplierCallResult(
            supplier_id=sid,
            outcome=raw.get("outcome") or "unclear",
            fields=raw.get("fields") or {},
            referral_leads=[
                ReferralLead(
                    name=x.get("name", "Unknown"),
                    phone=x.get("phone", "n/a"),
                    note=x.get("note"),
                    source_supplier_id=sid,
                )
                for x in (raw.get("referral_leads") or [])
            ],
            summary=raw.get("summary") or "",
            confidence=float(raw.get("confidence") or 0.9),
            transcript=raw.get("transcript") or [],
        )
        fields = result.fields
        for key in (
            "taking_new_medicare",
            "has_k0001",
            "serves_area",
            "delivery_eta_days",
            "responsive",
        ):
            if key in fields and fields[key] is not None:
                setattr(supplier, key, fields[key])
        supplier.call_attempts += 1
        supplier.last_contact_at = utc_now()
        supplier.transcript_summary = result.summary
        supplier.notes.append(result.summary)

        if result.outcome == "viable":
            supplier.status = "viable"
        elif result.outcome == "rejected":
            supplier.status = "rejected"
        elif result.outcome in {"voicemail", "no_answer", "callback"}:
            supplier.status = "awaiting_callback"
        else:
            supplier.status = "unresponsive"

        self.case.log("supplier", f"{supplier.id}:{result.outcome}", result.summary)
        for lead in result.referral_leads:
            self.add_supplier_from_lead(lead)

    def _uncontacted_referrals(self) -> list:
        return [
            s
            for s in self.case.suppliers
            if s.from_referral and s.status in {"not_contacted", "awaiting_callback"}
        ]

    async def talk_suppliers(self, supplier_ids: list[str]) -> None:
        rank_suppliers(self.case)
        names = []
        envelopes = []
        for sid in supplier_ids:
            s = self.case.get_supplier(sid)
            if not s:
                continue
            s.status = "calling"
            names.append(s.name)
            envelopes.append(
                Envelope(
                    sender="navigator",
                    recipient=f"supplier:{sid}",
                    kind="availability_check",
                    body={
                        "patient": self.case.patient.name,
                        "city": self.case.patient.city,
                        "hcpcs": self.case.equipment.hcpcs,
                        "plan": self.case.patient.plan,
                    },
                )
            )
        self._emit(f"Messaging {len(envelopes)} supplier agents → {', '.join(names)}")
        for i, envelope in enumerate(envelopes):
            if i > 0 and self._uncontacted_referrals():
                for skipped in envelopes[i:]:
                    sid = skipped.recipient.split(":", 1)[1]
                    shop = self.case.get_supplier(sid)
                    if shop and shop.status == "calling":
                        shop.status = "not_contacted"
                hot = ", ".join(s.name for s in self._uncontacted_referrals())
                self._emit(f"Got a referral — skipping rest of this batch, calling {hot} next")
                break
            raw = await self.bus.ask(envelope)
            if isinstance(raw, dict) and raw.get("transcript"):
                sid = raw.get("supplier_id", "?")
                s = self.case.get_supplier(sid)
                title = f"Local talk — {s.name if s else sid}"
                self._print_transcript(title, raw["transcript"])
                await self._maybe_speak(title, raw["transcript"])
            self.merge_supplier_result(raw)
            console.print(
                f"  [green]✓[/green] {raw.get('supplier_id')} → [bold]{raw.get('outcome')}[/bold] "
                f"— {raw.get('summary')}"
            )

    async def talk_clinic(self) -> None:
        self._emit("Messaging clinic reception agent")
        raw = await self.bus.ask(
            Envelope(
                sender="navigator",
                recipient="clinic",
                kind="chase_written_order",
                body={
                    "patient": self.case.patient.name,
                    "dob": self.case.patient.dob,
                    "hcpcs": self.case.equipment.hcpcs,
                    "current_status": self.case.pcp.order_status,
                },
            )
        )
        self._print_transcript("Local talk — Sunrise Family Medicine", raw.get("transcript") or [])
        await self._maybe_speak(
            f"clinic_{self.case.pcp.chase_attempts}",
            raw.get("transcript") or [],
        )
        self.case.pcp.chase_attempts += 1
        self.case.pcp.last_contact_at = utc_now()
        self.case.pcp.order_status = raw.get("order_status") or self.case.pcp.order_status
        self.case.pcp.notes.append(raw.get("summary") or "")
        if raw.get("order"):
            self.case.pcp.order = WrittenOrder(**raw["order"])
        self.case.log("clinic", raw.get("outcome", "chase"), raw.get("summary", ""))
        console.print(
            f"  [magenta]✓[/magenta] clinic → {raw.get('outcome')} "
            f"(order_status={raw.get('order_status')})"
        )

    async def talk_medicare(self) -> dict:
        chosen = self.case.get_supplier(self.case.selected_supplier_id or "")
        order = self.case.pcp.order
        raw = await self.bus.ask(
            Envelope(
                sender="navigator",
                recipient="medicare",
                kind="coverage_check",
                body={
                    "written_order": order is not None and self.case.pcp.order_status == "received",
                    "order_matches_k0001": bool(order and order.matches_request and order.hcpcs == "K0001"),
                    "supplier_medicare_enrolled": chosen is not None,
                    "supplier_can_fulfill": chosen is not None and chosen.status == "selected",
                    "hcpcs": self.case.equipment.hcpcs,
                },
            )
        )
        self._print_transcript("Local talk — Medicare Part B", raw.get("transcript") or [])
        await self._maybe_speak("medicare", raw.get("transcript") or [])
        self.case.log("medicare", "coverage_check", raw.get("summary", ""))
        return raw

    async def talk_patient(self, message: str) -> None:
        raw = await self.bus.ask(
            Envelope(
                sender="navigator",
                recipient="eleanor",
                kind="update",
                body={"message": message},
            )
        )
        self._print_transcript("Local talk — Eleanor", raw.get("transcript") or [])
        await self._maybe_speak("eleanor", raw.get("transcript") or [])
        self.case.patient_updates.append(PatientUpdate(message=message))
        self.case.patient.coinsurance_explained = True
        self.case.log("eleanor", "ack", raw.get("ack", ""))

    def handoff(self, supplier_id: str | None = None) -> None:
        viables = viable_suppliers(self.case)
        if supplier_id:
            chosen = self.case.get_supplier(supplier_id)
        else:
            chosen = None
        if not chosen:
            ranked = sorted(
                viables,
                key=lambda s: (s.delivery_eta_days is None, s.delivery_eta_days or 99),
            )
            chosen = ranked[0] if ranked else None
        if not chosen:
            self.escalate("no_supplier", "Handoff with no viable supplier", "Pick manually")
            return
        for s in self.case.suppliers:
            if s.id == chosen.id:
                s.status = "selected"
        self.case.selected_supplier_id = chosen.id
        eta = chosen.delivery_eta_days or 5
        self.case.delivery.status = "scheduled"
        self.case.delivery.scheduled_for = f"~{eta} business days after order sent"
        self.case.delivery.notes = f"Matched written order to {chosen.name}"
        self.case.status = "ready_for_handoff"
        self.case.log("navigator", "handoff", self.case.delivery.notes)
        self._emit(f"Handoff → {chosen.name} ({self.case.delivery.scheduled_for})")

    def escalate(self, reason: str, summary: str, recommended: str) -> None:
        self.case.human_needed = True
        self.case.status = "escalated"
        self.case.escalation = Escalation(
            reason=reason, summary=summary, recommended_human_action=recommended
        )
        self._emit(f"ESCALATE → {reason}")

    def complete(self) -> None:
        self.case.status = "completed"
        self.case.log("navigator", "complete", "Case completed in local multi-agent world")
        self._emit("Case completed ✅")

    def save(self) -> Path:
        OUTPUT.mkdir(exist_ok=True)
        path = OUTPUT / f"{self.case.case_id}.final.json"
        path.write_text(self.case.model_dump_json(indent=2))
        bus_path = OUTPUT / f"{self.case.case_id}.bus.json"
        bus_path.write_text(json.dumps(self.bus.transcript, indent=2))
        console.print(f"[dim]Wrote {path}[/dim]")
        console.print(f"[dim]Wrote {bus_path}[/dim]")
        if self.voice and not self.simulate:
            rec = OUTPUT / "recordings" / "current"
            rec.mkdir(parents=True, exist_ok=True)
            (rec / "bus.json").write_text(json.dumps(self.bus.transcript, indent=2))
            (rec / "final.json").write_text(self.case.model_dump_json(indent=2))
            (rec / "meta.json").write_text(
                json.dumps(
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "dialogue": llm_provider(),
                        "voice": "edge-tts",
                        "play": False,
                        "note": "Manager-only PCP chase. Replay via python -m src.demo",
                    },
                    indent=2,
                )
            )
            console.print(f"[dim]Stored demo recording in {rec}[/dim]")
        return path

    async def run(self) -> CaseState:
        console.print(
            Panel.fit(
                f"[bold]Local multi-agent world[/bold]\n"
                f"Patient: {self.case.patient.name}\n"
                f"Agents online: {', '.join(self.bus.agents())}",
                title="DME Care Navigator",
                border_style="bright_white",
            )
        )

        for step in range(1, self.max_steps + 1):
            console.rule(f"Navigator step {step}")
            decision = decide(self.case, max_parallel=self.max_parallel)
            self._emit(f"Decision: [bold]{decision.action}[/bold] — {decision.reason}")
            self.case.log("navigator", "decide", f"{decision.action}: {decision.reason}")

            if decision.action == "stop":
                break
            if decision.action == "dispatch_supplier_calls":
                ids = decision.supplier_ids or []
                if (
                    self.case.pcp.chase_attempts == 0
                    and self.case.pcp.order_status != "received"
                ):
                    # Parallel only in simulate mode; LLM mode stays sequential
                    # to avoid OpenAI rate-limit bursts.
                    if self.simulate:
                        self._emit("Fan-out: suppliers ∥ clinic")
                        import asyncio

                        await asyncio.gather(self.talk_suppliers(ids), self.talk_clinic())
                    else:
                        self._emit("Sequential LLM wave: suppliers then clinic")
                        await self.talk_suppliers(ids)
                        await self.talk_clinic()
                else:
                    await self.talk_suppliers(ids)
            elif decision.action == "dispatch_pcp_chase":
                await self.talk_clinic()
            elif decision.action == "request_handoff":
                sid = (decision.supplier_ids or [None])[0]
                self.handoff(sid)
                med = await self.talk_medicare()
                if not med.get("payable"):
                    self._emit(f"Medicare blockers: {med.get('blockers')}")
            elif decision.action == "notify_patient":
                chosen = self.case.get_supplier(self.case.selected_supplier_id or "")
                name = chosen.name if chosen else "the supplier"
                intent = (
                    f"Notify patient: Dr. Chen's written order is ready and {name} will deliver "
                    f"a standard manual wheelchair. Explain Original Medicare Part B ~20% coinsurance "
                    f"(no Medigap). Delivery timing: {self.case.delivery.scheduled_for}."
                )
                if self.simulate:
                    msg = (
                        f"Hi Eleanor — Dr. Chen's written order is ready and {name} will deliver "
                        f"your standard manual wheelchair. Under Original Medicare Part B with no "
                        f"Medigap, you typically owe about 20% coinsurance after the Part B deductible. "
                        f"Timing: {self.case.delivery.scheduled_for}."
                    )
                else:
                    composed = await compose_patient_reply(self.case, intent=intent)
                    msg = composed.get("reply") or intent
                await self.talk_patient(msg)
            elif decision.action == "complete":
                self.complete()
                break
            elif decision.action == "escalate":
                self.escalate(
                    decision.reason[:64],
                    decision.reason,
                    "Human care advocate should review bus transcript + case board",
                )
                break

            self._print_board()

        self.save()
        return self.case
