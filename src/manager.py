from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.models import (
    CaseState,
    Escalation,
    PatientUpdate,
    PCPCallResult,
    ReferralLead,
    SupplierCallResult,
    SupplierRecord,
    utc_now,
)
from src.policy import decide
from src.ranking import rank_suppliers, viable_suppliers
from src.workers.pcp import call_pcp
from src.workers.supplier import call_supplier
from src.workers.simulate import sim_call_pcp, sim_call_supplier

console = Console()
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUTPUT = ROOT / "output"


class CareManager:
    """
    Manager agent: owns case memory, ranks suppliers, dispatches voice workers
    in parallel, merges results, hands off / escalates.
    """

    def __init__(
        self,
        case: CaseState,
        *,
        max_parallel: int | None = None,
        simulate: bool = False,
    ):
        self.case = case
        self.max_parallel = max_parallel or int(os.getenv("MAX_PARALLEL_CALLS", "2"))
        self.max_steps = int(os.getenv("MAX_MANAGER_STEPS", "12"))
        self.simulate = simulate

    @classmethod
    def load_eleanor(cls, *, simulate: bool = False) -> CareManager:
        case_data = json.loads((DATA / "case_eleanor.json").read_text())
        suppliers_data = json.loads((DATA / "suppliers.json").read_text())
        case = CaseState.model_validate(case_data)
        case.suppliers = [SupplierRecord.model_validate(s) for s in suppliers_data]
        case.status = "in_progress"
        case.log("manager", "load_case", "Loaded Eleanor intake + sparse supplier directory")
        return cls(case, simulate=simulate)

    def _emit(self, msg: str) -> None:
        console.print(f"[bold cyan]MANAGER[/bold cyan] {msg}")

    def merge_supplier_result(self, result: SupplierCallResult) -> None:
        supplier = self.case.get_supplier(result.supplier_id)
        if not supplier:
            return

        fields = result.fields or {}
        for key in (
            "taking_new_medicare",
            "has_k0001",
            "serves_area",
            "delivery_eta_days",
            "responsive",
        ):
            if key in fields and fields[key] is not None:
                setattr(supplier, key, fields[key])

        supplier.transcript_summary = result.summary
        supplier.notes.append(result.summary)
        supplier.referral_leads.extend(result.referral_leads)
        supplier.last_contact_at = utc_now()

        if result.outcome == "viable":
            supplier.status = "viable"
        elif result.outcome in {"rejected"}:
            supplier.status = "rejected"
        elif result.outcome in {"no_answer", "voicemail"}:
            supplier.status = "awaiting_callback"
            supplier.responsive = False
        elif result.outcome == "callback":
            supplier.status = "awaiting_callback"
            supplier.next_followup_at = result.callback_at
        else:
            # unclear / evasive — treat Prairie-style as not yet viable
            if fields.get("taking_new_medicare") and fields.get("has_k0001") and fields.get("serves_area"):
                # said yes-ish but extractor wouldn't mark viable — keep hunting, mark unresponsive-leaning
                if "vague" in result.summary.lower() or "call back" in result.summary.lower():
                    supplier.status = "unresponsive"
                    supplier.notes.append("Sounded positive but would not commit — treating as unreliable")
                else:
                    supplier.status = "awaiting_callback"
            else:
                supplier.status = "rejected"

        self.case.log(
            "supplier_worker",
            f"call:{result.outcome}",
            f"{supplier.name}: {result.summary}",
        )

        for lead in result.referral_leads:
            self.add_supplier_from_lead(lead)

    def add_supplier_from_lead(self, lead: ReferralLead) -> None:
        # Dedup by phone digits
        digits = re.sub(r"\D", "", lead.phone)
        for existing in self.case.suppliers:
            same_phone = digits and re.sub(r"\D", "", existing.phone) == digits
            same_name = existing.name.lower() == lead.name.lower()
            if same_phone or same_name:
                existing.from_referral = True
                if lead.note:
                    existing.notes.append(lead.note)
                return

        new_id = f"lead_{len(self.case.suppliers) + 1:03d}"
        # Attach soft ground truth so a referred supplier can succeed in demo
        record = SupplierRecord(
            id=new_id,
            name=lead.name,
            phone=lead.phone,
            address=f"Chicago, IL (referral from {lead.source_supplier_id or 'supplier'})",
            from_referral=True,
            notes=[lead.note or "Referral lead"],
            ground_truth={
                "taking_new_medicare": True,
                "has_k0001": True,
                "serves_area": True,
                "delivery_eta_days": 5,
                "persona": "referred_shop",
                "behavior": "viable",
                "referral": None,
                "notes_for_actor": (
                    "You were referred. You take Original Medicare, stock K0001, serve Chicago, "
                    "delivery ~5 days after written order fax."
                ),
            },
        )
        self.case.suppliers.append(record)
        self.case.log(
            "manager",
            "add_lead",
            f"Added referral supplier {record.name} ({record.phone})",
        )
        self._emit(f"New lead stored → {record.name} ({record.phone})")

    def merge_pcp_result(self, result: PCPCallResult) -> None:
        self.case.pcp.chase_attempts += 1
        self.case.pcp.last_contact_at = utc_now()
        self.case.pcp.order_status = result.order_status
        self.case.pcp.notes.append(result.summary)
        if result.order:
            self.case.pcp.order = result.order
        if result.callback_at:
            self.case.pcp.next_followup_at = result.callback_at
        self.case.log("pcp_worker", f"chase:{result.outcome}", result.summary)

    async def dispatch_supplier_calls(self, supplier_ids: list[str]) -> None:
        suppliers = [self.case.get_supplier(i) for i in supplier_ids]
        suppliers = [s for s in suppliers if s is not None]
        if not suppliers:
            return

        rank_suppliers(self.case)
        names = ", ".join(s.name for s in suppliers)
        self._emit(f"Dispatching {len(suppliers)} parallel voice worker(s) → {names}")
        self.case.log("manager", "dispatch_supplier_calls", names)

        async def _one(s: SupplierRecord) -> SupplierCallResult:
            console.print(
                Panel.fit(
                    f"[bold]VOICE WORKER → SUPPLIER[/bold]\n{s.name} | {s.phone}",
                    border_style="green",
                )
            )
            result = await (sim_call_supplier(self.case, s) if self.simulate else call_supplier(self.case, s))
            self._print_transcript(s.name, result.transcript)
            return result

        results = await asyncio.gather(*[_one(s) for s in suppliers])
        for result in results:
            self.merge_supplier_result(result)
            console.print(
                f"  [green]✓[/green] {result.supplier_id} outcome=[bold]{result.outcome}[/bold] "
                f"— {result.summary}"
            )

    async def dispatch_pcp_chase(self) -> None:
        self._emit(f"Dispatching voice worker → PCP {self.case.pcp.name} @ {self.case.pcp.clinic}")
        console.print(
            Panel.fit(
                f"[bold]VOICE WORKER → PCP[/bold]\n{self.case.pcp.clinic} | {self.case.pcp.phone}",
                border_style="magenta",
            )
        )
        result = await (sim_call_pcp(self.case) if self.simulate else call_pcp(self.case))
        self._print_transcript(self.case.pcp.clinic, result.transcript)
        self.merge_pcp_result(result)
        console.print(
            f"  [magenta]✓[/magenta] PCP outcome=[bold]{result.outcome}[/bold] "
            f"order_status={result.order_status} — {result.summary}"
        )

    def request_handoff(self, supplier_id: str | None = None) -> None:
        viables = viable_suppliers(self.case)
        if supplier_id:
            chosen = self.case.get_supplier(supplier_id)
        else:
            chosen = viables[0] if viables else None
        if not chosen:
            self.escalate(
                "handoff_missing_supplier",
                "Tried to hand off but no viable supplier selected",
                "Manually pick a supplier from notes",
            )
            return

        # Prefer firm ETA
        ranked = sorted(
            viables or [chosen],
            key=lambda s: (s.delivery_eta_days is None, s.delivery_eta_days or 99),
        )
        chosen = ranked[0]
        for s in self.case.suppliers:
            if s.id == chosen.id:
                s.status = "selected"
            elif s.status == "viable":
                s.notes.append("Viable but not selected — keeping as backup")

        self.case.selected_supplier_id = chosen.id
        eta = chosen.delivery_eta_days or 5
        self.case.delivery.status = "scheduled"
        self.case.delivery.scheduled_for = f"~{eta} business days after order fax"
        self.case.delivery.notes = (
            f"Order ({self.case.equipment.hcpcs}) faxed/sent to {chosen.name}; "
            "billing code matched to written order."
        )
        self.case.status = "ready_for_handoff"
        detail = (
            f"Matched Dr. Chen written order → {chosen.name}. "
            f"Delivery window: {self.case.delivery.scheduled_for}"
        )
        self.case.log("manager", "handoff", detail)
        self._emit(detail)

    def notify_patient(self) -> None:
        chosen = self.case.get_supplier(self.case.selected_supplier_id or "")
        supplier_name = chosen.name if chosen else "the supplier"
        msg = (
            f"Hi Eleanor — Dr. Chen's written order is in and {supplier_name} will deliver "
            f"your standard manual wheelchair. Under Original Medicare Part B (no Medigap), "
            f"you typically owe about 20% coinsurance of the Medicare-approved amount after "
            f"your Part B deductible. Expected timing: {self.case.delivery.scheduled_for}."
        )
        self.case.patient_updates.append(PatientUpdate(message=msg))
        self.case.patient.coinsurance_explained = True
        self.case.log("manager", "notify_patient", msg)
        self._emit("Patient update queued (mock call)")
        console.print(Panel(msg, title="Patient call (mock)", border_style="blue"))

    def escalate(self, reason: str, summary: str, recommended: str) -> None:
        self.case.human_needed = True
        self.case.status = "escalated"
        self.case.escalation = Escalation(
            reason=reason,
            summary=summary,
            recommended_human_action=recommended,
        )
        self.case.log("manager", "escalate", f"{reason}: {summary}")
        self._emit(f"ESCALATE → {reason}: {summary}")

    def complete(self) -> None:
        self.case.status = "completed"
        self.case.log("manager", "complete", "Case completed without human intervention")
        self._emit("Case completed ✅")

    def _print_transcript(self, title: str, transcript: list[dict[str, str]]) -> None:
        table = Table(title=f"Call transcript — {title}", show_lines=False)
        table.add_column("Who", style="bold", width=12)
        table.add_column("Said")
        for turn in transcript:
            speaker = turn.get("speaker", "?")
            style = "cyan" if speaker == "advocate" else "yellow"
            table.add_row(f"[{style}]{speaker}[/{style}]", turn.get("text", ""))
        console.print(table)

    async def run(self) -> CaseState:
        console.print(
            Panel.fit(
                f"[bold]{self.case.patient.name}[/bold] — {self.case.equipment.description}\n"
                f"Goal: {self.case.goal}",
                title="DME Care Navigator",
                border_style="bright_white",
            )
        )

        for step in range(1, self.max_steps + 1):
            console.rule(f"Manager step {step}")
            decision = decide(self.case, max_parallel=self.max_parallel)
            self._emit(f"Decision: [bold]{decision.action}[/bold] — {decision.reason}")
            self.case.log("manager", "decide", f"{decision.action}: {decision.reason}")

            if decision.action == "stop":
                break
            if decision.action == "dispatch_supplier_calls":
                supplier_ids = decision.supplier_ids or []
                # True parallel: supplier voice workers + PCP chase when order still missing
                if (
                    self.case.pcp.order_status in {"verbal_only", "requested", "in_queue"}
                    and self.case.pcp.chase_attempts == 0
                ):
                    self._emit("Fan-out: supplier workers ∥ PCP chase")
                    await asyncio.gather(
                        self.dispatch_supplier_calls(supplier_ids),
                        self.dispatch_pcp_chase(),
                    )
                else:
                    await self.dispatch_supplier_calls(supplier_ids)
            elif decision.action == "dispatch_pcp_chase":
                await self.dispatch_pcp_chase()
            elif decision.action == "request_handoff":
                sid = (decision.supplier_ids or [None])[0]
                self.request_handoff(sid)
            elif decision.action == "notify_patient":
                self.notify_patient()
            elif decision.action == "complete":
                self.complete()
                break
            elif decision.action == "escalate":
                self.escalate(
                    reason=decision.reason[:64],
                    summary=decision.reason,
                    recommended="Care advocate to review event_log and intervene",
                )
                break

            self._print_board()

        self.save()
        return self.case

    def _print_board(self) -> None:
        table = Table(title="Case board")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("status", self.case.status)
        table.add_row("PCP order", self.case.pcp.order_status)
        table.add_row(
            "viable suppliers",
            ", ".join(s.name for s in viable_suppliers(self.case)) or "—",
        )
        table.add_row("selected", self.case.selected_supplier_id or "—")
        table.add_row("delivery", self.case.delivery.status)
        table.add_row("human_needed", str(self.case.human_needed))
        console.print(table)

    def save(self) -> Path:
        OUTPUT.mkdir(exist_ok=True)
        # Strip ground_truth from saved external view? Keep for debugging demo.
        path = OUTPUT / f"{self.case.case_id}.final.json"
        path.write_text(self.case.model_dump_json(indent=2))
        console.print(f"[dim]Wrote {path}[/dim]")
        return path
