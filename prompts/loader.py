from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROMPTS = Path(__file__).resolve().parent


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_supplier_prompt(supplier_id: str) -> dict[str, Any]:
    """Per-supplier persona + knowledge bank under prompts/suppliers/."""
    return _read_json(PROMPTS / "suppliers" / f"{supplier_id}.json")


def load_clinic_knowledge() -> dict[str, Any]:
    return _read_json(PROMPTS / "case" / "clinic.json")


def attach_supplier_knowledge(supplier: dict[str, Any]) -> dict[str, Any]:
    """
    Merge prompts/suppliers/{id}.json into supplier['ground_truth'].
    Directory fields (name, phone, address) stay in data/suppliers.json.
    """
    bank = load_supplier_prompt(supplier["id"])
    if not bank:
        return supplier
    knowledge = dict(bank.get("knowledge") or {})
    gt = {
        **knowledge,
        "notes_for_actor": bank.get("actor_notes") or knowledge.get("notes_for_actor", ""),
    }
    if bank.get("persona"):
        gt["persona"] = bank["persona"]
    supplier = dict(supplier)
    supplier["ground_truth"] = gt
    return supplier


def clinic_ground_truth(case_pcp: dict[str, Any]) -> dict[str, Any]:
    bank = load_clinic_knowledge()
    if not bank:
        return case_pcp.get("ground_truth") or {}
    gt = dict(bank.get("knowledge") or {})
    if bank.get("persona"):
        gt["persona"] = bank["persona"]
    if bank.get("actor_notes"):
        gt["notes_for_actor"] = bank["actor_notes"]
    return gt


def hydrate_suppliers(suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [attach_supplier_knowledge(dict(s)) for s in suppliers]


def hydrate_case(case_data: dict[str, Any]) -> dict[str, Any]:
    case_data = dict(case_data)
    pcp = dict(case_data.get("pcp") or {})
    pcp["ground_truth"] = clinic_ground_truth(pcp)
    case_data["pcp"] = pcp
    return case_data
