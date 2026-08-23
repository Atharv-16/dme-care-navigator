"""Agent system prompts and per-party knowledge banks."""

from prompts.loader import (
    attach_supplier_knowledge,
    hydrate_case,
    hydrate_suppliers,
    load_clinic_knowledge,
    load_supplier_prompt,
)

__all__ = [
    "attach_supplier_knowledge",
    "hydrate_case",
    "hydrate_suppliers",
    "load_clinic_knowledge",
    "load_supplier_prompt",
]
