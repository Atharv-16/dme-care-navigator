from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROMPTS = Path(__file__).resolve().parent
PERSONAS = PROMPTS / "personas"
KNOWLEDGE = PROMPTS / "knowledge"

PARTY_IDS = (
    ["navigator", "eleanor", "clinic", "medicare"]
    + [f"sup_{i:03d}" for i in range(1, 13)]
)

CALLEE_PHONE_RULES = (
    "## Phone rules (callee)\n"
    "- First line: greeting only (your organization name + how can I help).\n"
    "- Do not answer the request, quote inventory, or say [END] on that greeting.\n"
    "- Wait for the caller. Answer only what they asked.\n"
    "- When the call is done: one-sentence goodbye, then [END]."
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_persona(party_id: str, *, mode: str | None = None) -> str:
    """Read prompts/personas/{party_id}.md. Navigator supports mode=clinic|supplier."""
    path = PERSONAS / f"{party_id}.md"
    if not path.exists():
        return ""
    text = path.read_text().strip()
    if party_id == "navigator" and mode:
        pattern = rf"<!-- mode:{mode} -->\s*(.*?)(?=<!-- mode:|$)"
        match = re.search(pattern, text, flags=re.S)
        if match:
            return match.group(1).strip()
    # Drop navigator section markers if reading whole file
    text = re.sub(r"<!-- mode:\w+ -->\s*", "", text)
    return text.strip()


def load_knowledge(party_id: str) -> dict[str, Any]:
    return _read_json(KNOWLEDGE / f"{party_id}.json")


def _format_persona(template: str, ctx: dict[str, Any]) -> str:
    try:
        return template.format(**ctx)
    except KeyError:
        return template


def build_system_prompt(
    party_id: str,
    *,
    side: str = "callee",
    mode: str | None = None,
    extra: str = "",
    **ctx: Any,
) -> str:
    """Self-contained system prompt: persona markdown + static knowledge bank only."""
    persona = _format_persona(load_persona(party_id, mode=mode), ctx)
    bank = load_knowledge(party_id)
    parts: list[str] = []
    if persona:
        parts.append(persona)
    elif side == "callee" and party_id.startswith("sup_"):
        parts.append(CALLEE_PHONE_RULES)
    if extra:
        parts.append(extra)
    notes = bank.get("actor_notes")
    if notes:
        parts.append(f"Actor guidance: {notes}")
    facts = bank.get("facts")
    if facts is not None:
        parts.append(f"Ground truth: {json.dumps(facts)}")
    return "\n\n".join(parts)


def clinic_opener(*, clinic_name: str) -> str:
    return (
        f"The phone rang at {clinic_name}. Pick up. "
        "Greeting only: clinic name and how can I help. Do not mention the order yet."
    )


def supplier_opener(*, supplier_name: str) -> str:
    return (
        f"The phone rang at {supplier_name}. Pick up. "
        "Greeting only: say the shop name and ask how you can help. "
        "Do not mention wheelchairs, Medicare, stock, or ETAs until the caller asks."
    )


def clinic_touch_extra(*, touches: int) -> str:
    bank = load_knowledge("clinic")
    rules = bank.get("touch_rules") or {}
    if touches == 0:
        return f"FIRST CALL: {rules.get('first', 'Order not signed yet.')}"
    return f"FOLLOW-UP CALL: {rules.get('follow_up', 'Order is signed and ready to collect.')}"


def attach_supplier_knowledge(supplier: dict[str, Any]) -> dict[str, Any]:
    bank = load_knowledge(supplier["id"])
    if not bank:
        return supplier
    facts = dict(bank.get("facts") or {})
    gt = {
        **facts,
        "notes_for_actor": bank.get("actor_notes", ""),
        "persona": load_persona(supplier["id"]),
    }
    supplier = dict(supplier)
    supplier["ground_truth"] = gt
    return supplier


def clinic_ground_truth(_case_pcp: dict[str, Any]) -> dict[str, Any]:
    bank = load_knowledge("clinic")
    gt = dict(bank.get("facts") or {})
    if bank.get("actor_notes"):
        gt["notes_for_actor"] = bank["actor_notes"]
    gt["persona"] = load_persona("clinic")
    gt["touch_rules"] = bank.get("touch_rules")
    return gt


def hydrate_suppliers(suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [attach_supplier_knowledge(dict(s)) for s in suppliers]


def hydrate_case(case_data: dict[str, Any]) -> dict[str, Any]:
    case_data = dict(case_data)
    pcp = dict(case_data.get("pcp") or {})
    pcp["ground_truth"] = clinic_ground_truth(pcp)
    case_data["pcp"] = pcp
    return case_data


def eleanor_system(*, patient_name: str) -> str:
    persona = load_persona("eleanor")
    bank = load_knowledge("eleanor")
    parts = [
        persona.replace("Eleanor Martinez", patient_name),
        f"Ground truth: {json.dumps(bank.get('facts', {}))}",
        'Respond as JSON {"understood":true,"ack":"short spoken reply","confused":false}.',
    ]
    if bank.get("actor_notes"):
        parts.insert(1, f"Actor guidance: {bank['actor_notes']}")
    return "\n\n".join(parts)
