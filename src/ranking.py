from __future__ import annotations

from src.models import CaseState, SupplierRecord


def score_supplier(case: CaseState, supplier: SupplierRecord) -> float:
    """Deterministic ranking — keep Medicare policy out of the LLM."""
    score = 0.0
    city = case.patient.city.lower()

    if "chicago" in supplier.address.lower() or "chicago" in city:
        score += 2.0

    if supplier.from_referral:
        score += 4.0

    if supplier.status == "not_contacted":
        score += 3.0
    elif supplier.status == "awaiting_callback":
        score += 2.5
    elif supplier.status == "calling":
        score += 1.0
    elif supplier.status == "viable":
        score += 5.0
    elif supplier.status in {"rejected", "unresponsive", "selected"}:
        score -= 10.0

    # Prefer unknowns over known failures; mild boost if prior positives
    if supplier.taking_new_medicare is True:
        score += 1.0
    if supplier.has_k0001 is True:
        score += 1.0
    if supplier.serves_area is True:
        score += 1.0
    if supplier.taking_new_medicare is False:
        score -= 3.0
    if supplier.has_k0001 is False:
        score -= 3.0
    if supplier.serves_area is False:
        score -= 3.0

    # Fewer attempts first
    score -= 0.35 * supplier.call_attempts
    return score


def rank_suppliers(case: CaseState) -> list[SupplierRecord]:
    ranked = sorted(case.suppliers, key=lambda s: score_supplier(case, s), reverse=True)
    for s in ranked:
        s.priority_score = score_supplier(case, s)
    return ranked


def next_supplier_batch(case: CaseState, limit: int = 2) -> list[SupplierRecord]:
    ranked = rank_suppliers(case)
    eligible: list[SupplierRecord] = []
    for s in ranked:
        if s.status in {"rejected", "unresponsive", "selected", "viable", "calling"}:
            continue
        eligible.append(s)
    referred = [s for s in eligible if s.from_referral]
    if referred:
        return referred[:limit]
    return eligible[:limit]


def viable_suppliers(case: CaseState) -> list[SupplierRecord]:
    return [s for s in case.suppliers if s.status == "viable"]


def exhausted_suppliers(case: CaseState) -> bool:
    if not case.suppliers:
        return False
    return all(s.status in {"rejected", "unresponsive"} for s in case.suppliers)
