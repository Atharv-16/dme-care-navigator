"""One persona (.md) + one knowledge bank (.json) per party under prompts/."""

from prompts.loader import (
    PARTY_IDS,
    attach_supplier_knowledge,
    build_system_prompt,
    hydrate_case,
    hydrate_suppliers,
    load_knowledge,
    load_persona,
)

__all__ = [
    "PARTY_IDS",
    "attach_supplier_knowledge",
    "build_system_prompt",
    "hydrate_case",
    "hydrate_suppliers",
    "load_knowledge",
    "load_persona",
]
