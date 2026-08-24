from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROMPTS = Path(__file__).resolve().parent

PARTY_IDS = (
    ["navigator", "patient", "clinic", "medicare"]
    + [f"sup_{i:03d}" for i in range(1, 13)]
)

NAVIGATOR_JSON = (
    "Return ONLY valid JSON with keys reply and conclusion: "
    '{"reply":"spoken words for TTS this turn","conclusion":""}. '
    "No markdown fences. Put [END] or [NO_ANSWER] inside reply when hanging up. "
    "On a live phone turn, conclusion is usually empty. "
    "After a call (analyze), put the self-contained case log in conclusion."
)

OTHER_JSON = (
    'Return ONLY valid JSON with one key: {"reply":"spoken words for TTS this turn"}. '
    "No conclusion key. No markdown fences. "
    "Put [END] or [NO_ANSWER] inside reply when hanging up."
)

LIVE_SPOKEN = (
    "You are on a LIVE PHONE CALL. Output ONLY the words you say out loud. "
    "Plain spoken English. No JSON. No curly braces. No markdown. "
    "Keep it to ONE or TWO short sentences. Ask ONE question, then stop and wait. "
    "Do not dump date of birth, address, and the full case in one breath. "
    "When the call is actually done, one-sentence goodbye then [END]."
)


def _party_paths(party_id: str) -> tuple[Path, Path]:
    if party_id in {"eleanor", "patient"}:
        folder = PROMPTS / "patient"
        return folder / "persona.md", folder / "knowledge.json"
    if party_id in {"navigator", "clinic", "medicare"}:
        folder = PROMPTS / party_id
        return folder / "persona.md", folder / "knowledge.json"
    if party_id.startswith("sup_"):
        return PROMPTS / "suppliers" / f"{party_id}.md", PROMPTS / "suppliers" / f"{party_id}.json"
    folder = PROMPTS / party_id
    return folder / "persona.md", folder / "knowledge.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_persona(party_id: str, *, mode: str | None = None) -> str:
    """Read the party's persona.md. Navigator supports mode=clinic|supplier|analyze."""
    md_path, _ = _party_paths(party_id)
    if not md_path.exists():
        return ""
    text = md_path.read_text().strip()
    if party_id == "navigator" and "<!-- mode:" in text:
        parts = re.split(r"(?=<!-- mode:)", text, maxsplit=1)
        header = parts[0].strip()
        rest = parts[1] if len(parts) > 1 else ""
        sections: dict[str, str] = {}
        for match in re.finditer(
            r"<!-- mode:(\w+) -->\s*(.*?)(?=<!-- mode:|$)",
            rest,
            flags=re.S,
        ):
            sections[match.group(1)] = match.group(2).strip()
        if mode:
            body = sections.get(mode, "")
            return f"{header}\n\n{body}".strip() if body else header
        return header
    return text


def load_knowledge(party_id: str) -> dict[str, Any]:
    _, json_path = _party_paths(party_id)
    return _read_json(json_path)


def build_system_prompt(
    party_id: str,
    *,
    side: str = "callee",
    mode: str | None = None,
    extra: str = "",
    spoken: bool = False,
    **_ctx: Any,
) -> str:
    """Self-contained system prompt: persona markdown + static knowledge + output rule."""
    if (
        spoken
        and party_id == "navigator"
        and mode in {None, "clinic", "live_clinic"}
    ):
        live_path = PROMPTS / "navigator" / "live_clinic.md"
        if live_path.exists():
            text = live_path.read_text().strip()
            if extra:
                text = f"{text}\n\n{extra}"
            return text

    persona = load_persona(party_id, mode=mode)
    bank = load_knowledge(party_id)
    parts: list[str] = []
    if spoken:
        parts.append(LIVE_SPOKEN)
    if persona:
        parts.append(persona)
    if extra:
        parts.append(extra)
    notes = bank.get("actor_notes")
    if notes:
        parts.append(f"Actor guidance: {notes}")
    facts = bank.get("facts")
    if facts is not None and not spoken:
        parts.append(f"Ground truth: {json.dumps(facts)}")
    if spoken:
        parts.append(LIVE_SPOKEN)
    elif party_id == "navigator":
        parts.append(NAVIGATOR_JSON)
    else:
        parts.append(OTHER_JSON)
    return "\n\n".join(parts)


def clinic_opener(*, clinic_name: str = "Sunrise Family Medicine") -> str:
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
